import { randomUUID } from "node:crypto";
import { configure } from "norma-sdk";

const application = {
  async *stream(sessionId: string, turnId: string): AsyncGenerator<string> {
    // The config reads these values to identify the turn being checked.
    void sessionId;
    void turnId;
    yield "Hello from ";
    yield "a streamed response.";
  },
};

async function main(): Promise<void> {
  const configured = await configure("norma-agents", { bindings: { application } });
  try {
    const sessionId = randomUUID();
    const turnId = randomUUID();
    for await (const delta of application.stream(sessionId, turnId)) {
      process.stdout.write(delta);
    }
    process.stdout.write("\n");
  } finally {
    await configured.close();
  }
}

void main();
