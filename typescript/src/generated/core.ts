// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import { ClientSDK } from "./lib/sdks.js";

/**
 * A minimal client to use when calling standalone SDK functions. Typically, an
 * instance of this class would be instantiated once at the start of an
 * application and passed around through some dependency injection mechanism  to
 * parts of an application that need to make SDK calls.
 */
export class NormaSDKCore extends ClientSDK {}
