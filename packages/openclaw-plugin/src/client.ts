import type {
  ExecutionRegistrationRequest,
  ExecutionRegistrationResponse,
  ExecutionTelemetryResponse,
  PluginConfig,
  ResourceScope,
  ToolBeforeRequest,
  ToolCompletedEvent,
  ToolDecision
} from "./contracts.js";
import {sidecarRequestHeaders} from "./sidecar-auth.js";

export class SidecarClient {
  constructor(private readonly config: PluginConfig) {}

  async decide(payload: ToolBeforeRequest): Promise<ToolDecision> {
    return this.post<ToolDecision>("/v1/decisions/tool", payload, this.config.decisionTimeoutMs, 3);
  }

  async reportCompletion(payload: ToolCompletedEvent): Promise<void> {
    await this.post<unknown>("/v1/events/tool-completed", payload, this.config.reportTimeoutMs, 3);
  }

  async reportModel(payload: unknown): Promise<void> {
    await this.post<unknown>("/v1/events/model", payload, this.config.reportTimeoutMs, 3);
  }

  async registerExecution(payload: ExecutionRegistrationRequest): Promise<ExecutionRegistrationResponse> {
    return this.post<ExecutionRegistrationResponse>("/v2/executions", payload, this.config.decisionTimeoutMs, 3);
  }

  async getExecutionScope(executionId: string): Promise<ResourceScope | null> {
    const response = await this.get<{execution_scope: ResourceScope | null}>(
      `/v2/executions/${encodeURIComponent(executionId)}/scope`,
      this.config.reportTimeoutMs
    );
    return response.execution_scope;
  }

  async getExecutionTelemetry(executionId: string): Promise<unknown | null> {
    const response = await this.get<ExecutionTelemetryResponse>(
      `/v2/executions/${encodeURIComponent(executionId)}/telemetry`,
      this.config.reportTimeoutMs
    );
    return response.tool_resource;
  }

  private async post<T>(path: string, payload: unknown, timeoutMs: number, attempts = 1): Promise<T> {
    return this.request<T>(path, {method: "POST", body: JSON.stringify(payload)}, timeoutMs, attempts);
  }

  private async get<T>(path: string, timeoutMs: number): Promise<T> {
    return this.request<T>(path, {method: "GET"}, timeoutMs);
  }

  private async request<T>(path: string, init: RequestInit, timeoutMs: number, attempts = 1): Promise<T> {
    let lastError: unknown = null;
    for (let attempt = 0; attempt < attempts; attempt++) {
      try {
        return await this.requestOnce<T>(path, init, timeoutMs);
      } catch (error) {
        lastError = error;
        if (attempt + 1 >= attempts || !isRetryableSidecarError(error)) throw error;
        await new Promise(resolve => setTimeout(resolve, 50 * (attempt + 1)));
      }
    }
    throw lastError;
  }

  private async requestOnce<T>(path: string, init: RequestInit, timeoutMs: number): Promise<T> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${this.config.endpoint}${path}`, {
        ...init,
        headers: sidecarRequestHeaders(),
        signal: controller.signal
      });
      if (!response.ok) {
        throw new Error(`sidecar_http_${response.status}: ${await responseErrorPreview(response)}`);
      }
      return (await response.json()) as T;
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") {
        throw new Error(`sidecar_timeout_${timeoutMs}ms`);
      }
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }
}

function isRetryableSidecarError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  if (error.message.startsWith("sidecar_timeout_")) return true;
  const match = /^sidecar_http_(\d+)/.exec(error.message);
  if (match) {
    const status = Number(match[1]);
    return status === 408 || status === 429 || status >= 500;
  }
  // fetch() network failures are TypeError in Node.  They are safe to retry
  // because every POST endpoint above is keyed idempotently.
  return error instanceof TypeError;
}

async function responseErrorPreview(response: Response): Promise<string> {
  try {
    const text = await response.text();
    return text.slice(0, 1000);
  } catch {
    return response.statusText || "no response body";
  }
}
