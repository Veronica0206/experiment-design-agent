#!/usr/bin/env node
/** Validator-only inspector for the hook runtime's fixed domain-tool policy. */

import { domainPolicyDocument } from "./domain_tool_policy.mjs";

if (process.argv.length !== 2) {
  process.stderr.write("Domain policy inspection accepts no arguments.\n");
  process.exit(2);
}

process.stdout.write(`${JSON.stringify(domainPolicyDocument())}\n`);
