import { defineConfig } from 'vite';

export default defineConfig({
  build: {
    rollupOptions: {
      input: {
        main: 'index.html',
        search: 'search.html',
        seed: 'seed.html'
      }
    }
  },
  plugins: [
    {
      // Em produção o servidor estático resolve /search para search.html; aqui o dev server faz o mesmo.
      name: 'search-route',
      configureServer(server) {
        server.middlewares.use((req, _res, next) => {
          if (req.url === '/search' || req.url.startsWith('/search?')) {
            req.url = '/search.html' + req.url.slice('/search'.length);
          }
          next();
        });
      }
    }
  ]
});
