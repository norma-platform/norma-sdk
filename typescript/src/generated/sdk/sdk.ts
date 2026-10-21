// Modified for Norma.
import { ClientSDK } from "../lib/sdks.js";
import { Agents } from "./agents.js";
import { RuleBindings } from "./rule-bindings.js";
import { Checks } from "./checks.js";
import { Evaluators } from "./evaluators.js";
import { Observability } from "./observability.js";

/** Allowlisted hosted transport. This client contains no evaluator implementation. */
export class NormaSDK extends ClientSDK {
  private _agents?: Agents;
  get agents(): Agents { return (this._agents ??= new Agents(this._options)); }
  private _ruleBindings?: RuleBindings;
  get ruleBindings(): RuleBindings { return (this._ruleBindings ??= new RuleBindings(this._options)); }
  private _checks?: Checks;
  get checks(): Checks { return (this._checks ??= new Checks(this._options)); }
  private _evaluators?: Evaluators;
  get evaluators(): Evaluators { return (this._evaluators ??= new Evaluators(this._options)); }
  private _observability?: Observability;
  get observability(): Observability { return (this._observability ??= new Observability(this._options)); }
}
