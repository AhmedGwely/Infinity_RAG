FROM node:22-alpine AS build
WORKDIR /w
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN node build.mjs

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /w/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
