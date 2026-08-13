export type Logger = {
  warn(message: string, data?: unknown): void;
  info(message: string, data?: unknown): void;
  error(message: string, data?: unknown): void;
};

export const consoleLogger: Logger = {
  warn: (message, data) => console.warn(message, data ?? ""),
  info: (message, data) => console.info(message, data ?? ""),
  error: (message, data) => console.error(message, data ?? "")
};
