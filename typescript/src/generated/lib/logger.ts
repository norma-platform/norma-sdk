// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

export interface Logger {
  group(label?: string): void;
  groupEnd(): void;
  log(message: any, ...args: any[]): void;
}
