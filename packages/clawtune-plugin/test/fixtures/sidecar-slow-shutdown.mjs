import {writeFileSync} from "node:fs";
import {createServer} from "node:http";
import {join} from "node:path";

const [port, directory] = process.argv.slice(2);
const server = createServer((_request, response) => {
  response.writeHead(200, {"content-type": "application/json"}).end(JSON.stringify({
    schema_version: "clawtune.health.v1", service: "clawtune-sidecar", live: true,
  }));
});
process.once("SIGTERM", () => {
  server.close();
  server.closeAllConnections();
  // Simulate snapshot serialization that exceeds the old force-kill budget.
  setTimeout(() => {
    writeFileSync(join(directory, "saved"), "committed\n");
    process.exit(0);
  }, 4_000);
});
server.listen(Number(port), "127.0.0.1", () => {
  writeFileSync(join(directory, "pid"), String(process.pid));
});
