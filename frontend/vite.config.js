import { defineConfig } from 'vite';

export default defineConfig({
  build: {
    rollupOptions: {
      input: {
        main: 'index.html',
        search: 'search.html',
        mcp: 'mcp.html',
        seed: 'seed.html'
      }
    }
  },
  plugins: [
    {
      // Em produção o Caddy resolve /search para search.html e /mcp para mcp.html; aqui o dev server faz o mesmo.
      name: 'html-routes',
      configureServer(server) {
        server.middlewares.use((req, _res, next) => {
          const m = req.url.match(/^\/(search|mcp)(\?.*)?$/);
          if (m) req.url = `/${m[1]}.html${m[2] ?? ''}`;
          next();
        });
      }
    }
  ]
});
