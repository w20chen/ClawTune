declare module "openclaw/plugin-sdk/plugin-entry" {
  type JsonSchema = Record<string, unknown>;

  export type HookApi = {
    id: string;
    pluginConfig?: Record<string, unknown>;
    on(name: string | string[], handler: (event: unknown, context?: unknown) => unknown | Promise<unknown>, opts?: {priority?: number; timeoutMs?: number}): void;
    logger?: {
      error(message: string, data?: unknown): void;
      warn(message: string, data?: unknown): void;
      info(message: string, data?: unknown): void;
      debug(message: string, data?: unknown): void;
    };
  };

  export function definePluginEntry(options: {
    id: string;
    name: string;
    description: string;
    configSchema?: JsonSchema;
    register(api: HookApi): void;
  }): unknown;
}
