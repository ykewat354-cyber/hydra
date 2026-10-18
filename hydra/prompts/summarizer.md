Compress the conversation prefix into a state digest. Output exactly these
sections: GOAL, STATUS, DECISIONS, FILES (path — state), COMMANDS (cmd → outcome),
FINDINGS, BLOCKERS, NEXT. Max ~900 tokens. Preserve exact paths, identifiers,
URLs and error strings. Drop pleasantries, retries and raw dumps.
