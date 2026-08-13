/**
 * Trace sanitizer — redacts sensitive data from trace records.
 *
 * Operates on a COPY of the data; never modifies the original values
 * that are passed to the actual tool executor.
 *
 * Handles:
 *  - Field-name-based redaction (token, api_key, secret, password, etc.)
 *  - Command-line token patterns (--token=..., --token ..., Authorization: Bearer ...)
 *  - Environment variable patterns (OPENAI_API_KEY=..., ANTHROPIC_API_KEY=..., etc.)
 */

const REDACTED = "<redacted>";

const SENSITIVE_KEY_PATTERNS = [
  /token/i,
  /api[_-]?key/i,
  /apikey/i,
  /secret/i,
  /password/i,
  /passwd/i,
  /authorization/i,
  /auth/i,
  /cookie/i,
  /credential/i,
  /private[_-]?key/i,
  /access[_-]?key/i,
  /bearer/i,
];

const SENSITIVE_ENV_VAR_PATTERNS = [
  /^OPENAI_API_KEY$/i,
  /^ANTHROPIC_API_KEY$/i,
  /^AWS_SECRET_ACCESS_KEY$/i,
  /^AWS_ACCESS_KEY_ID$/i,
  /^GEMINI_API_KEY$/i,
  /^COHERE_API_KEY$/i,
  /^AZURE_OPENAI_API_KEY$/i,
  /^CLAWTUNE_TOKEN$/i,
  /^CLAWTUNE_TOKEN$/i,
  /^OPENCLAW_.*/i,
  /^CLAWTUNE_.*/i,
  /^GITHUB_TOKEN$/i,
  /^GH_TOKEN$/i,
  /^NPM_TOKEN$/i,
  /^DOCKER_PASSWORD$/i,
  /^DOCKER_TOKEN$/i,
];

const BEARER_PATTERN = /\bBearer\s+[A-Za-z0-9+/=._-]{8,}\b/gi;
const TOKEN_FLAG_PATTERN = /(--token[= ])\S+/gi;
const HIGH_PRIVILEGE_MESSAGE_ROLES = new Set(["system", "developer"]);

function isSensitiveKey(key: string): boolean {
  return SENSITIVE_KEY_PATTERNS.some((p) => p.test(key));
}

function isSensitiveEnvVar(key: string): boolean {
  return SENSITIVE_ENV_VAR_PATTERNS.some((p) => p.test(key));
}

/**
 * Recursively sanitize a value by redacting sensitive keys.
 * Returns a NEW object/array; never mutates the input.
 */
export function sanitizeTraceData(value: unknown): unknown {
  if (value === null || value === undefined) return value;
  if (typeof value === "string") {
    return sanitizeString(value);
  }
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
    return value;
  }
  if (Array.isArray(value)) {
    if (looksLikeMessageArray(value)) {
      return sanitizeMessageArray(value);
    }
    return value.map((item) => sanitizeTraceData(item));
  }
  if (typeof value === "object") {
    const output: Record<string, unknown> = {};
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      if (isSensitiveKey(key)) {
        output[key] = REDACTED;
      } else if (key === "env" && typeof child === "object" && child !== null) {
        output[key] = sanitizeEnvObject(child as Record<string, unknown>);
      } else if (key === "messages" && Array.isArray(child)) {
        output[key] = sanitizeMessageArray(child);
      } else {
        output[key] = sanitizeTraceData(child);
      }
    }
    return output;
  }
  return value;
}

/**
 * Sanitize a command string: replace --token=... and --token ... patterns,
 * plus Bearer tokens.
 */
export function sanitizeString(value: string): string {
  let result = value;
  result = result.replace(BEARER_PATTERN, "Bearer " + REDACTED);
  result = result.replace(TOKEN_FLAG_PATTERN, (_match, prefix: string) => {
    return prefix + REDACTED;
  });
  return result;
}

/**
 * Sanitize an env object: redact known sensitive env vars.
 */
function sanitizeEnvObject(env: Record<string, unknown>): Record<string, unknown> {
  const output: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(env)) {
    if (isSensitiveEnvVar(key) || isSensitiveKey(key)) {
      output[key] = REDACTED;
    } else if (typeof value === "string") {
      output[key] = sanitizeString(value);
    } else {
      output[key] = value;
    }
  }
  return output;
}

function looksLikeMessageArray(value: unknown[]): boolean {
  return value.length > 0 && value.every((item) => {
    if (typeof item !== "object" || item === null || Array.isArray(item)) return false;
    const role = (item as Record<string, unknown>).role;
    return typeof role === "string";
  });
}

function sanitizeMessageArray(messages: unknown[]): unknown[] {
  return messages.map((message) => {
    if (typeof message !== "object" || message === null || Array.isArray(message)) {
      return sanitizeTraceData(message);
    }

    const record = message as Record<string, unknown>;
    const role = typeof record.role === "string" ? record.role.toLowerCase() : "";
    const output: Record<string, unknown> = {};
    for (const [key, child] of Object.entries(record)) {
      if (key === "content" && shouldRedactMessageContent(role)) {
        output[key] = REDACTED;
      } else {
        output[key] = sanitizeTraceData(child);
      }
    }
    return output;
  });
}

function shouldRedactMessageContent(role: string): boolean {
  return role.length === 0 || HIGH_PRIVILEGE_MESSAGE_ROLES.has(role);
}

/**
 * Check if a string contains any non-redacted token-like patterns.
 * Used by the trace validator to detect possible leaks.
 */
export function containsPossibleSecret(value: unknown): boolean {
  if (typeof value === "string") {
    // After sanitization, the string should not contain Bearer tokens
    // or --token= patterns with actual values
    const bearerMatch = value.match(/Bearer\s+[A-Za-z0-9+/=._-]{8,}/i);
    if (bearerMatch && !value.includes(REDACTED)) return true;
    const tokenMatch = value.match(/--token[= ]\s*[A-Za-z0-9+/=._-]{4,}/i);
    if (tokenMatch && !value.includes(REDACTED)) return true;
    return false;
  }
  if (Array.isArray(value)) {
    return value.some((item) => containsPossibleSecret(item));
  }
  if (typeof value === "object" && value !== null) {
    return Object.values(value as Record<string, unknown>).some((v) =>
      containsPossibleSecret(v),
    );
  }
  return false;
}
