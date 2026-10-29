/** Resolve omitted decision tags without modifying authored JSON or explicit values. */
export function inferSemanticDecisionTypes<T extends Record<string, unknown>>(config: T): T {
  const isObject = (value: unknown): value is Record<string, unknown> =>
    value !== null && typeof value === "object" && !Array.isArray(value);
  if (!Array.isArray(config.questions) || !isObject(config.decision) || !Array.isArray(config.decision.rules)) return config;
  const types = new Map<string, string | null>();
  for (const question of config.questions) {
    if (!isObject(question) || typeof question.id !== "string") continue;
    const type = question.type;
    types.set(question.id, types.has(question.id) ? null :
      type === "statement" || type === "choice" || type === "score" ? type : null);
  }
  const rules = config.decision.rules.map((rule: unknown) => {
    if (!isObject(rule) || Object.hasOwn(rule, "type") || typeof rule.question_id !== "string") return rule;
    const type = types.get(rule.question_id);
    return type ? { ...rule, type } : rule;
  });
  return { ...config, decision: { ...config.decision, rules } };
}
