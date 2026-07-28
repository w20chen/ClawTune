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
    return this.post<ToolDecision>("/v1/decisions/tool", payload, this.config.decisionTimeoutMs);
  }

  async reportCompletion(payload: ToolCompletedEvent): Promise<void> {
    await this.post<unknown>("/v1/events/tool-completed", payload, this.config.reportTimeoutMs);
  }

  async reportModel(payload: unknown): Promise<void> {
    await this.post<unknown>("/v1/events/model", payload, this.config.reportTimeoutMs);
  }

  async registerExecution(payload: ExecutionRegistrationRequest): Promise<ExecutionRegistrationResponse> {
    return this.post<ExecutionRegistrationResponse>("/v2/executions", payload, this.config.decisionTimeoutMs);
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

  private async post<T>(path: string, payload: unknown, timeoutMs: number): Promise<T> {
    return this.request<T>(path, {method: "POST", body: JSON.stringify(payload)}, timeoutMs);
  }

  private async get<T>(path: string, timeoutMs: number): Promise<T> {
    return this.request<T>(path, {method: "GET"}, timeoutMs);
  }

  private async request<T>(path: string, init: RequestInit, timeoutMs: number): Promise<T> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${this.config.endpoint}${path}`, {
        ...init,
        headers: sidecarRequestHeaders(),
        signal: controller.signal
      });
      if (!response.ok) {
        throw new Error(`sidecar_http_${response.status}`);
      }
      return (await response.json()) as T;
    } finally {
      clearTimeout(timeout);
    }
  }
}
