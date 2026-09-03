/// <reference types="vite/client" />
const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';
const LIMIT = 20;

interface Result {
    url: string;
    title: string | null;
    host: string;
    snippet: string | null;
    summary: string | null;
    score: number;
    rank: number;
    published: string | null;
    bm25_rank: number | null;
    vector_rank: number | null;
}

interface Page {
    offset: number;
    has_more: boolean;
    results: Result[];
    timings: Record<string, number>;
    warnings: string[];
}

document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('search-form') as HTMLFormElement;
    const input = document.getElementById('search-input') as HTMLInputElement;
    const results = document.getElementById('search-results') as HTMLDivElement;
    const loading = document.getElementById('loading') as HTMLDivElement;
    const noResults = document.getElementById('no-results') as HTMLDivElement;
    const summaries = document.getElementById('summaries') as HTMLInputElement;
    summaries.checked = localStorage.getItem('summaries') !== 'off';
    summaries.addEventListener('change', () => localStorage.setItem('summaries', summaries.checked ? 'on' : 'off'));

    // sentinela no fim da lista: quando aparece na tela, pede a próxima página
    const sentinel = document.createElement('div');
    sentinel.className = 'text-muted text-center py-3';
    results.after(sentinel);
    let current = '';
    let offset = 0;
    let hasMore = false;
    let busy = false;
    new IntersectionObserver((entries) => {
        if (entries[0].isIntersecting && hasMore && !busy) search(current, offset);
    }).observe(sentinel);

    const query = new URLSearchParams(window.location.search).get('q')?.trim();
    if (query) {
        input.value = query;
        start(query);
    } else {
        loading.classList.add('d-none');
        noResults.classList.remove('d-none');
    }

    form.addEventListener('submit', (e) => {
        e.preventDefault();
        const q = input.value.trim();
        if (!q) return;
        window.history.pushState({ q }, '', `${window.location.pathname}?q=${encodeURIComponent(q)}`);
        results.innerHTML = '';
        noResults.classList.add('d-none');
        loading.classList.remove('d-none');
        start(q);
    });

    function start(q: string) {
        current = q;
        offset = 0;
        hasMore = false;
        sentinel.textContent = '';
        search(q, 0);
    }

    async function search(q: string, from: number) {
        busy = true;
        if (from > 0) sentinel.textContent = 'Carregando mais...';
        try {
            const started = performance.now();
            const params = `q=${encodeURIComponent(q)}&limit=${LIMIT}&offset=${from}&summaries=${summaries.checked}`;
            const r = await fetch(`${API_URL}/search?${params}`);
            if (!r.ok) {
                const body = await r.json().catch(() => null);
                throw new Error(body?.detail ?? `A API respondeu ${r.status}.`);
            }
            const page = (await r.json()) as Page;
            if (q !== current) return; // o usuário já buscou outra coisa
            loading.classList.add('d-none');
            if (from === 0) {
                results.innerHTML = page.warnings
                    .map((w) => `<div class="alert alert-warning">${w}</div>`)
                    .join('');
            }
            if (from === 0 && page.results.length === 0) {
                noResults.classList.remove('d-none');
                return;
            }
            if (from === 0) {
                const ms = Math.round(performance.now() - started);
                const parts = Object.entries(page.timings)
                    .filter(([k]) => k !== 'total')
                    .map(([k, v]) => `${k} ${v} ms`)
                    .join(' · ');
                results.insertAdjacentHTML(
                    'beforeend',
                    `<div class="text-muted small mb-3">${ms} ms no total · ${parts}</div>`,
                );
            }
            for (const item of page.results) results.appendChild(render(item));
            offset = from + page.results.length;
            hasMore = page.has_more;
            sentinel.textContent = hasMore ? '' : `Fim dos resultados: ${offset}`;
        } catch (err) {
            console.error(err);
            loading.classList.add('d-none');
            const text = err instanceof TypeError ? `Sem resposta da API em ${API_URL}.` : (err as Error).message;
            const msg = `<div class="alert alert-danger">${text}</div>`;
            if (from === 0) results.innerHTML = msg;
            else sentinel.innerHTML = msg;
        } finally {
            busy = false;
        }
    }

    function render(item: Result): HTMLDivElement {
        const el = document.createElement('div');
        el.className = 'result-item';
        const favicon = `https://www.google.com/s2/favicons?domain=${item.host}&sz=32`;
        const ranks = [
            item.bm25_rank ? `BM25 #${item.bm25_rank}` : null,
            item.vector_rank ? `vetor #${item.vector_rank}` : null,
        ].filter(Boolean).join(' · ');
        el.innerHTML = `
            <div class="result-url-container">
                <img src="${favicon}" class="result-favicon" alt="">
                <a href="${item.url}" class="result-url">${escape(item.url)}</a>
            </div>
            <a href="${item.url}" class="result-title">
                ${escape(item.title ?? item.host)}
                <span class="similarity-badge">${ranks} · rank ${item.rank}${item.published ? ` · ${item.published}` : ''}</span>
            </a>
            ${item.summary ? `<div class="result-snippet"><strong>Resumo:</strong> ${escape(item.summary)}</div>` : ''}
            ${item.snippet ? `<div class="result-snippet text-muted small mt-1">${escape(item.snippet)}</div>` : ''}
            <form class="input-group input-group-sm mt-2" style="max-width: 560px">
                <input type="text" class="form-control" placeholder="Pergunte algo sobre esta página">
                <button class="btn btn-outline-secondary" type="submit">Perguntar</button>
            </form>
            <div class="result-snippet mt-2"></div>
        `;
        const form = el.querySelector('form')!;
        const answer = el.querySelector('div.mt-2:last-child') as HTMLDivElement;
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const prompt = (form.querySelector('input') as HTMLInputElement).value.trim();
            if (!prompt) return;
            answer.textContent = 'Perguntando...';
            const started = performance.now();
            try {
                const url = `${API_URL}/page?url=${encodeURIComponent(item.url)}&prompt=${encodeURIComponent(prompt)}`;
                const r = await fetch(url);
                const data = (await r.json()) as { answer: string | null; detail?: string };
                const ms = Math.round(performance.now() - started);
                answer.innerHTML = `<strong>Resposta</strong> <span class="text-muted small">(${ms} ms)</span>: ${escape(data.answer ?? data.detail ?? 'sem resposta')}`;
            } catch (err) {
                answer.textContent = `Erro: ${err}`;
            }
        });
        return el;
    }

    function escape(s: string): string {
        return s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]!);
    }
});
