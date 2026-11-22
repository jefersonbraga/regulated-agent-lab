# UI image: builds the Vite app, then serves the static bundle with nginx and strict security
# headers (docker/nginx.conf.template). VITE_API_URL is baked in at build time and resolved by
# the *browser*; API_ORIGIN (its origin) is the only other origin the CSP lets the page call.
# A relative VITE_API_URL (e.g. /api, the API behind the same reverse proxy: infra/) gives an
# empty API_ORIGIN, i.e. connect-src 'self' only (docker/api-origin.envsh).
FROM node:22-alpine AS build
RUN mkdir -p /app && chown node:node /app
WORKDIR /app
USER node
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci
COPY --chown=node:node . .
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=${VITE_API_URL}
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
ARG VITE_API_URL=http://localhost:8000
# API_ORIGIN for the CSP's connect-src is derived from this at container start (an API_ORIGIN
# set at run time overrides it, e.g. if the API moves).
ENV VITE_API_URL=${VITE_API_URL}
COPY --chmod=0755 docker/api-origin.envsh /docker-entrypoint.d/14-api-origin.envsh
# The image runs envsubst on /etc/nginx/templates/*.template into /etc/nginx/conf.d/.
COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY docker/security-headers.conf.template /etc/nginx/templates/security-headers.conf.template
USER root
RUN mkdir -p /etc/nginx/snippets \
    && ln -sf /etc/nginx/conf.d/security-headers.conf /etc/nginx/snippets/security-headers.conf
USER nginx
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
