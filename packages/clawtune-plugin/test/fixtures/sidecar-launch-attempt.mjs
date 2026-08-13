import {appendFileSync} from "node:fs";

appendFileSync(process.argv[2], "attempt\n", "utf8");
process.exit(17);
