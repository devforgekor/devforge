FROM node:20-slim

RUN apt-get update && apt-get install -y \
    ghostscript fonts-nanum tini && apt-get clean
RUN ln -s /usr/share/fonts/truetype/nanum /usr/share/ghostscript/fonts/

RUN useradd -m -u 1001 -s /bin/bash appuser

WORKDIR /app
COPY package*.json ./
RUN npm install --production && npm cache clean --force

COPY . .
RUN chown -R appuser:appuser /app
RUN chmod 1777 /tmp

USER appuser
EXPOSE 80

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "index.js"]