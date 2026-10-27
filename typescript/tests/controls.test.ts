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
