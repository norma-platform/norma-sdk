// Modified for Norma.
import { NormaSDK } from "./generated/sdk/sdk";
import { HTTPClient } from "./generated/lib/http";
import { buildCheckEvents, CheckReportQueue } from "./internal/check-events";
import { HostedOperationsApi, type OperationsApi } from "./internal/operations";
import type { ChecksApi, AgentsApi, RuleBindingsApi, EvaluatorsApi, ObservabilityApi } from "./public-api";
import {createChecks, decisionFromWire, publicApis} from "./internal/public-adapters";
export type { AgentsApi, RuleBindingsApi, EvaluatorsApi, ObservabilityApi } from "./public-api";
import type { DeliveryReceipt } from "./check-events";
import type { Logger } from "./generated/lib/logger";

export type { StepSchema } from "./generated/models/step-schema";

export type APIKeyProvider = string | (() => Promise<string>);

export interface NormaInitOptions {
  agentName: string;
  targetType?: string;
  targetId?: string;
  runtimeUrl?: string;
  agentKey?: APIKeyProvider;
  timeoutMs?: number;
  /** Report observed rule results to this Norma server. Reporting never changes decisions. */
  observabilityEnabled?: boolean;
  userAgent?: string;
  debugLogger?: Logger;
  httpClient?: HTTPClient;
}

export type NormaConfig = Omit<NormaInitOptions, "agentName" | "runtimeUrl"> & {
  agentName: string; runtimeUrl: string;
};

function environment(): Record<string, string | undefined> {
  return typeof process !== "undefined" && process.env ? process.env : {};
}

const runtimes = new WeakMap<NormaClient, NormaSDK>();
export function checkFor(client: NormaClient): NormaSDK["checks"] {
  const sdk = runtimes.get(client);
  if (!sdk) throw new Error("NormaClient is not initialized.");
  return sdk.checks;
}
const connections = new WeakMap<NormaClient, NormaConfig>();
/** Internal transport configuration, deliberately absent from the package exports. */
export function connectionFor(client: NormaClient): NormaConfig | undefined { return connections.get(client); }

export class NormaClient {
  private revision = 0;
  /** Changes before every initialization attempt, invalidating outstanding integration permissions. */
  get connectionRevision(): number { return this.revision; }

  private options: NormaConfig | null = null;
  private sdk: NormaSDK | null = null;
  private operationsApi: OperationsApi | null = null;
  private readonly reports = new CheckReportQueue();

  /** Configure one agent. Registration and rule attachment remain explicit server operations. */
  init(input: NormaInitOptions): void {
    this.revision += 1;
    // A rejected reconfiguration must never silently keep a previous agent identity active.
    this.options = null; this.sdk = null; this.operationsApi = null;
    connections.delete(this); runtimes.delete(this);
    const accepted = new Set(["agentName", "targetType", "targetId", "runtimeUrl", "agentKey",
      "timeoutMs", "observabilityEnabled", "userAgent", "debugLogger", "httpClient"]);
    if (!input || Object.keys(input).some(key => !accepted.has(key))) {
      throw new TypeError("Unsupported Norma connection option. Use agentName, runtimeUrl and agentKey; account identity is server-owned.");
    }
    const env = environment();
    const agentName = input.agentName;
    const runtimeUrl = input.runtimeUrl ?? env.NORMA_RUNTIME_URL;
    const credential = input.agentKey ?? env.NORMA_AGENT_KEY;
    const agentKey = typeof credential === "function" ? async () => {
      const key = await credential();
      if (typeof key !== "string" || !key.trim()) throw new TypeError("Norma Agent Key provider returned an empty key.");
      return key;
    } : credential;
    if (!agentName?.trim()) throw new TypeError("Provide agentName for your registered Norma agent.");
    if (!runtimeUrl?.trim()) throw new TypeError("Set NORMA_RUNTIME_URL or provide runtimeUrl from your Norma connection settings.");
    if (!agentKey || (typeof agentKey === "string" && !agentKey.trim())) throw new TypeError("Set NORMA_AGENT_KEY or provide agentKey.");
    let endpoint: URL;
    try { endpoint = new URL(runtimeUrl); } catch { throw new TypeError("Norma runtimeUrl must be an absolute HTTP or HTTPS URL."); }
    if ((endpoint.protocol !== "https:" && !(endpoint.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(endpoint.hostname))) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
      throw new TypeError("Norma runtimeUrl must use HTTPS (HTTP only for loopback development), without credentials, query or fragment.");
    }
    const options: NormaConfig = { ...input, agentName, runtimeUrl, ...(agentKey === undefined ? {} : { agentKey }) };
    if (Boolean(options.targetType) !== Boolean(options.targetId)) {
      throw new Error("targetType and targetId must be supplied together.");
    }
    this.options = { ...options };
    connections.set(this, this.options);
    this.operationsApi = null;
    const httpClient = options.httpClient?.clone() ?? new HTTPClient();
    const prefix = endpoint.pathname.replace(/\/$/, "") + "/";
    httpClient.addHook("beforeRequest", (request) => {
      const destination = new URL(request.url);
      if (destination.origin !== endpoint.origin || !destination.pathname.startsWith(prefix)) {
        throw new Error("A request cannot override the configured Norma runtime endpoint.");
      }
      for (const name of request.headers.keys()) {
        if (request.headers.get(name) && name !== "x-user-agent" && /^(?:x-(?:tenant|user|account|customer|namespace)(?:-|$)|authorization$|cookie$)/i.test(name)) {
          throw new TypeError(`Identity header ${name} cannot override Agent Key authorization.`);
        }
      }
      return new Request(request, { redirect: "error" });
    });
    this.sdk = new NormaSDK({
      serverURL: options.runtimeUrl,
      apiKeyHeader: options.agentKey,
      timeoutMs: options.timeoutMs,
      userAgent: options.userAgent,
      debugLogger: options.debugLogger,
      httpClient,
    });
    const sdk = this.sdk;
    runtimes.set(this, sdk);
    const evaluate = sdk.checks.check.bind(sdk.checks);
    sdk.checks.check = async (request, requestOptions) => {
      // Check failures retain their original behavior. Only the reporting
      // boundary is best-effort, including event construction and sink failures.
      const response = await evaluate({ ...request, executionMode: "hosted" }, requestOptions);
      if (response.executionContract !== "hosted-v1") {
        throw new Error("Unsupported Norma runtime: hosted-v1 execution proof is required before an action can proceed.");
      }
      decisionFromWire(response);
      if (options.observabilityEnabled !== false) {
        try {
          const events = buildCheckEvents(request, response);
          this.reports.enqueue(events, (batch) => sdk.observability.ingestEvents(
            { events: batch },
            { serverURL: requestOptions?.serverURL, headers: requestOptions?.headers,
              timeoutMs: 2000, retries: { strategy: "none" } },
          ));
        } catch {
          // Report construction failure without including payloads or evaluator evidence.
          this.reports.reportingFailed([response.matches, response.nonMatches, response.errors, response.unresolved]
            .reduce((count, values) => count + (values?.length ?? 0), 0));
        }
      }
      return response;
    };
  }

  /** Drain best-effort observations before exit; failures never rethrow as decisions. */
  async flushObservability(): Promise<DeliveryReceipt> {
    return this.reports.flush(this.options?.observabilityEnabled === false);
  }

  toJSON(): { initialized: boolean; config: ReturnType<NormaClient["safeConfig"]> } {
    return { initialized: this.initialized, config: this.safeConfig() };
  }

  private safeConfig() { return this.config; }

  get initialized(): boolean {
    return this.sdk !== null;
  }

  get config(): Omit<NormaConfig, "agentKey" | "httpClient" | "debugLogger"> | null {
    if (!this.options) return null;
    const { agentKey: _key, httpClient: _http, debugLogger: _logger, ...publicConfig } = this.options;
    void _key; void _http; void _logger;
    return publicConfig;
  }

  get agents(): AgentsApi {
    return publicApis(this.requireSDK()).agents;
  }


  get ruleBindings(): RuleBindingsApi {
    return publicApis(this.requireSDK()).ruleBindings;
  }


  /** Runtime checks for this configured agent; this method never executes a tool. */
  get checks(): ChecksApi { return createChecks(checkFor(this), this.options!); }


  get operations(): OperationsApi {
    const sdk = this.requireSDK();
    if (!this.operationsApi) {
      this.operationsApi = new HostedOperationsApi(sdk, {
        agentName: this.options!.agentName,
        ...(this.options!.targetType === undefined ? {} : {
          targetType: this.options!.targetType, targetId: this.options!.targetId,
        }),
      });
    }
    return this.operationsApi;
  }

  get evaluators(): EvaluatorsApi {
    return publicApis(this.requireSDK()).evaluators;
  }

  get observability(): ObservabilityApi {
    return publicApis(this.requireSDK()).observability;
  }



  private requireSDK(): NormaSDK {
    if (!this.sdk) {
      throw new Error(
        "NormaClient is not initialized. Call init(...) before making API calls.",
      );
    }

    return this.sdk;
  }
}

/** Shared client used by the functional helpers after `norma.init(...)`. */
export const defaultClient = new NormaClient();
