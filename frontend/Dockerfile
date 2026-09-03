FROM node:24-slim AS build
WORKDIR /app
ARG VITE_API_URL=https://api.shiftsad.dev
ENV VITE_API_URL=$VITE_API_URL
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile
