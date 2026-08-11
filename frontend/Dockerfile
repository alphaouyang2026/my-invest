# Dev-oriented image: source is bind-mounted in by docker-compose. Not a
# production build — that's out of scope until this actually gets deployed
# somewhere other than the developer's own machine (spec §14.3: localhost only).
FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm install

COPY . .

CMD ["npm", "run", "dev"]
