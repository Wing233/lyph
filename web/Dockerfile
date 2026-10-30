FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS dependencies
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
FROM dependencies AS builder
ENV NEXT_TELEMETRY_DISABLED=1 API_BASE_URL=http://api:8000 NODE_OPTIONS=--max-old-space-size=512
COPY web/ ./
RUN npm run build
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000 API_BASE_URL=http://api:8000
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public
COPY --chown=node:node deploy/release-server.mjs ./release-server.mjs
USER node
EXPOSE 3000
CMD ["node", "release-server.mjs"]
