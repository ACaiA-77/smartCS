# SmartCS pi-harness — agent runtime container (Phase 9 §2).
#
# Runs the SAME entry point the local process deployment uses
# (`tsx src/server/main.ts`), so the two deployment shapes cannot drift: the
# container is packaging, not a different runtime. `tsx` is a devDependency but
# IS the runtime here, so `npm ci` installs the full locked tree — the pi
# version and its transitive dependencies stay pinned by package-lock.json.
FROM node:22-slim

ENV NODE_ENV=production \
    PORT=8971 \
    HOST=0.0.0.0 \
    SMARTCS_RUNTIME_CWD=/app/.runtime/cwd \
    SMARTCS_PI_SESSION_DIR=/app/.runtime/pi-sessions \
    SMARTCS_PI_AGENT_DIR=/app/.runtime/pi-agent

WORKDIR /app

# Dependencies first: this layer only rebuilds when the lockfile changes.
# `--include=dev` is required, not incidental: with NODE_ENV=production npm
# would skip devDependencies, and `tsx` (the runtime used by CMD) is one of
# them. The locked tree is installed exactly as `npm ci` resolves it.
COPY package.json package-lock.json ./
RUN npm ci --include=dev --no-audit --no-fund

COPY tsconfig.json ./
COPY src ./src
COPY skills ./skills

# Non-root, and the runtime dirs exist in the image so a fresh named volume
# inherits app ownership instead of root.
RUN mkdir -p /app/.runtime/cwd /app/.runtime/pi-sessions /app/.runtime/pi-agent \
    && chown -R node:node /app

USER node

EXPOSE 8971

# No curl in slim: probe with node itself. A container that cannot answer
# /health is not serving, and the orchestrator should say so.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||8971)+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "node_modules/tsx/dist/cli.mjs", "src/server/main.ts"]
