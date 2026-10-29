import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import { validateAuthoredControls } from '../src/controls';

const fixture = JSON.parse(
  readFileSync(new URL('../../tests/fixtures/controls/control-cases.json', import.meta.url), 'utf8'),
) as {
  capabilities: unknown;
  cases: Array<{
    name: string;
    source: string | Record<string, unknown>;
    source_as_mapping?: boolean;
    without_capabilities?: boolean;
    capabilities?: unknown;
    status: string;
    source_validation: string;
    requires_server_validation?: boolean;
    diagnostic_code?: string;
    reference_projection?: {
      source_pointer: string;
      destination_pointer: string | null;
      evaluator_pointer: string | null;
    };
  }>;
};

describe('shared Console control helper fixtures', () => {
  for (const example of fixture.cases) {
    it(example.name, () => {
      const result = validateAuthoredControls(
        example.source,
        example.without_capabilities ? undefined : example.capabilities ?? fixture.capabilities,
      );

      expect(result.status).toBe(example.status);
      expect(result.sourceValidation).toBe(example.source_validation);
      expect(result.requiresServerValidation).toBe(
        example.requires_server_validation === true || example.status === 'requires_server_validation',
      );
      if (example.diagnostic_code) {
        expect(result.diagnostics.map((diagnostic) => diagnostic.code)).toContain(example.diagnostic_code);
      }
      if (example.reference_projection) {
        expect(result.references).toHaveLength(1);
        expect(result.references[0]).toEqual({
          key: 'limit',
          sourcePointer: example.reference_projection.source_pointer,
          destinationPointer: example.reference_projection.destination_pointer,
          evaluatorPointer: example.reference_projection.evaluator_pointer,
        });
      }
    });
  }

  it('returns recursively frozen authored copies', () => {
    const example = fixture.cases[0];
    const result = validateAuthoredControls(example.source, fixture.capabilities);
    expect(result.authoredDocument).not.toBeNull();
    expect(Object.isFrozen(result.authoredDocument)).toBe(true);
    expect(Object.isFrozen(result.authoredDocument?.controls)).toBe(true);
  });

  it('accepts implicit hosted execution without rewriting the authored document', () => {
    // Given: a controlled rule with the default execution omitted.
    const document = JSON.parse(fixture.cases[0].source as string) as Record<string, unknown>;
    delete document.execution;

    // When: checking controls against trusted capabilities.
    const result = validateAuthoredControls(JSON.stringify(document), fixture.capabilities);

    // Then: validation remains delegated to the server without expanding the source.
    expect(result.status).toBe('requires_server_validation');
    expect(result.authoredDocument).toEqual(document);
    expect(result.references).toHaveLength(1);
  });

  it.each([null, 'sdk', 'other'])('rejects an explicit unsupported execution %s', (execution) => {
    // Given: explicit unsupported execution is different from omission.
    const document = JSON.parse(fixture.cases[0].source as string) as Record<string, unknown>;
    document.execution = execution;

    // When: checking the document.
    const result = validateAuthoredControls(JSON.stringify(document), fixture.capabilities);

    // Then: the helper does not silently replace the explicit execution choice.
    expect(result.status).toBe('invalid');
    expect(result.diagnostics.map((item) => item.code)).toContain('control_execution_unsupported');
  });

  it.each([false, true])('infers semantic decision types with explicit selector %s without rewriting source', (withSelector) => {
    // Given: a threshold control with the semantic question type specified once.
    const document = {
      condition: {
        ...(withSelector ? { selector: { path: '*' } } : {}),
        evaluator: { name: 'semantic', config: {
          bindings: [{ name: 'evidence', path: 'input', required: false }],
          questions: [{ id: 'q', type: 'statement', instructions: 'Check evidence',
            criteria: { true: 'Supported', false: 'Unsupported' } }],
          decision: { on_unresolved: 'deny', rules: [{ question_id: 'q', threshold: { $param: 'limit' } }] },
        } },
      },
      controls: { limit: { type: 'number', label: 'Threshold', default: 0.8, minimum: 0, maximum: 1 } },
    };
    const capabilities = {
      contract: 'rule-controls-v1', fingerprint: 'c'.repeat(64), manifests: [{
        evaluator: 'semantic', evaluator_version: '1', schema_fingerprint: 'a'.repeat(64), slots: [{
          evaluator: 'semantic', evaluator_version: '1', schema_fingerprint: 'a'.repeat(64),
          path_template: '/config/decision/rules/*/threshold', control_types: ['number'],
          predicates: [['/config/decision/rules/*/type', 'statement']], applicability: [],
          affects: 'decision', requires_server_validation: true,
          expected_value_schema: { types: ['number'], minimum: 0, maximum: 1 },
        }],
      }],
    };

    // When: validating against the question-specific trusted destination.
    const result = validateAuthoredControls(JSON.stringify(document), capabilities);

    // Then: source, nondefaults, and authored reference pointers remain exact.
    expect(result.status).toBe('requires_server_validation');
    expect(result.authoredDocument).toEqual(document);
    expect(result.references).toEqual([{
      key: 'limit', sourcePointer: '/condition/evaluator/config/decision/rules/0/threshold',
      destinationPointer: '/condition/evaluator/config/decision/rules/0/threshold',
      evaluatorPointer: '/condition/evaluator',
    }]);
  });

  it.each(['raw', 'parsed'])('keeps a rejected %s authored document immutable', (inputKind) => {
    const source = { execution: 'server', parameters: { nested: ['original'] } };
    const result = validateAuthoredControls(
      inputKind === 'raw' ? JSON.stringify(source) : source,
      undefined,
    );

    expect(result.status).toBe('invalid');
    expect(result.authoredDocument).not.toBeNull();
    expect(Object.isFrozen(result.authoredDocument)).toBe(true);
    expect(Object.isFrozen(result.authoredDocument?.parameters)).toBe(true);
    expect(Reflect.set(result.authoredDocument ?? {}, 'execution', 'changed')).toBe(false);
    source.parameters.nested.push('changed');
    expect(result.authoredDocument?.parameters).toEqual({ nested: ['original'] });
  });

  it.each([
    { name: 'below minimum', value: [], minimum: 2, maximum: 5, code: 'control_value_out_of_bounds' },
    { name: 'above maximum', value: ['a', 'b'], minimum: 0, maximum: 1, code: 'control_value_out_of_bounds' },
    { name: 'inverted bounds', value: ['a'], minimum: 5, maximum: 2, code: 'control_declaration_invalid' },
  ])('rejects list defaults or bounds outside the declaration: $name', ({ value, minimum, maximum, code }) => {
    const capabilities = {
      contract: 'rule-controls-v1', fingerprint: 'c'.repeat(64), manifests: [{
        evaluator: 'json', evaluator_version: '3.2.1', schema_fingerprint: 'a'.repeat(64),
        slots: [{
          evaluator: 'json', evaluator_version: '3.2.1', schema_fingerprint: 'a'.repeat(64),
          path_template: '/config/tags', control_types: ['string_list'],
          predicates: [], applicability: [], affects: 'decision',
          expected_value_schema: { types: ['string_list'], min_items: 0, max_items: 128 },
          requires_server_validation: false,
        }],
      }],
    };
    const source = {
      execution: 'server',
      condition: { selector: { path: 'input' }, evaluator: {
        name: 'json', config: { tags: { $param: 'tags' } },
      } },
      controls: { tags: {
        type: 'string_list', label: 'Tags', default: value,
        min_items: minimum, max_items: maximum,
      } },
    };

    const result = validateAuthoredControls(JSON.stringify(source), capabilities);

    expect(result.status).toBe('invalid');
    expect(result.diagnostics.map((diagnostic) => diagnostic.code)).toContain(code);
  });

  it('does not invoke accessors while admitting parsed object input', () => {
    let invoked = false;
    const source = Object.defineProperty({}, 'execution', {
      enumerable: true,
      get() {
        invoked = true;
        throw new Error('accessor must not run');
      },
    });
    const result = validateAuthoredControls(source, undefined);
    expect(invoked).toBe(false);
    expect(result.status).toBe('invalid');
    expect(result.sourceValidation).toBe('invalid');
  });

  it('preserves __proto__ as parsed JSON data and normalizes parsed negative zero', () => {
    const proto = validateAuthoredControls('{"__proto__":{"safe":true}}', undefined);
    expect(Object.hasOwn(proto.authoredDocument ?? {}, '__proto__')).toBe(true);
    expect(proto.authoredDocument?.['__proto__']).toEqual({ safe: true });

    const source = JSON.parse('{"controls":{"zero":{"type":"number","label":"Zero","default":-0.0,"minimum":-1,"maximum":1}},"execution":"server"}') as Record<string, unknown>;
    const parsed = validateAuthoredControls(source, undefined);
    const controls = parsed.authoredDocument?.controls as Record<string, { default: number }>;
    expect(Object.is(controls.zero.default, -0)).toBe(false);
    expect(controls.zero.default).toBe(0);
  });

  it('rejects retired root bindings while preserving arbitrary evaluator JSON', () => {
    const base = { execution: 'server', condition: {}, action: { decision: 'deny' } };
    for (const field of ['template', 'template_values', 'parameters']) {
      const result = validateAuthoredControls({ ...base, [field]: {} }, undefined);
      expect(result.status).toBe('invalid');
      expect(result.diagnostics[0]?.sourcePointer).toBe(`/${field}`);
    }
    const nested = { ...base, condition: { evaluator: { name: 'json', config: {
      parameters: { customer: { threshold: 4 } },
    } } } };
    const nestedResult = validateAuthoredControls(nested, undefined);
    expect(nestedResult.status).not.toBe('invalid');
    expect(nestedResult.authoredDocument?.condition).toEqual(nested.condition);
  });
});
