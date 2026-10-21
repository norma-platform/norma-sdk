// Modified for Norma.
/** JSON validators that retain every own key, including literal __proto__ data. */
import * as z from "zod/v4-mini";
import { copyCheckpointArguments } from "./checkpoint-arguments";
import type { JsonObject, JsonValue } from "./types";

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
export const jsonValueSchema = z.custom<JsonValue>((value) => {
  try { copyCheckpointArguments([value], false); return true; } catch { return false; }
});
export const jsonObjectSchema = z.custom<JsonObject>((value) => object(value) && jsonValueSchema.safeParse(value).success);

/** Parse each own entry without treating literal prototype-named keys as object behavior. */
export const recordSchema = <T extends z.ZodMiniType>(schema: T) =>
  z.pipe(z.custom<Record<string, z.input<T>>>((value) => object(value)
    && Object.values(value).every((entry) => schema.safeParse(entry).success)),
  z.transform(value => Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, schema.parse(entry)])) as Record<string, z.output<T>>));
