import sdk = require("norma-sdk");
const client = new sdk.NormaClient();
client.operations.get("operation-id");
sdk.default satisfies sdk.NormaClient;

const statement: sdk.SemanticStatementAnswer = { type: "statement", support_score: 0.8 };
const view: sdk.SemanticStatementAnswer = {type: "statement", support_score: 0.8};
view.support_score satisfies number;
