/// <reference types="vite/client" />
import { voice } from './voice';

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
    corrected: string | null;
}

const form = document.getElementById('search-form') as HTMLFormElement;
const input = document.getElementById('search-input') as HTMLInputElement;
const status = document.getElementById('status') as HTMLParagraphElement;
const results = document.getElementById('results') as HTMLDivElement;
const more = document.getElementById('more') as HTMLParagraphElement;
const timings = document.getElementById('timings') as HTMLElement;
const summaries = document.getElementById('summaries') as HTMLButtonElement;

let current = '';
let offset = 0;
let hasMore = false;
let busy = false;

// Resumos: a chave só troca o que aparece; ligada de novo, refaz a busca se faltar resumo.
const summariesOn = () => summaries.getAttribute('aria-checked') === 'true';
function setSummaries(on: boolean) {
    summaries.setAttribute('aria-checked', String(on));
    document.body.classList.toggle('no-sum', !on);
    localStorage.setItem('summaries', on ? 'on' : 'off');
}
setSummaries(localStorage.getItem('summaries') !== 'off');
summaries.addEventListener('click', () => {
    setSummaries(!summariesOn());
    if (summariesOn() && current && results.querySelector('.text.nosum')) start(current);
});

voice(document.getElementById('mic') as HTMLButtonElement, input, () => form.requestSubmit());

// Sentinela no fim da lista: quando aparece na tela, pede a próxima página.
new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && hasMore && !busy) search(current, offset);
}).observe(more);

form.addEventListener('submit', (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    window.history.pushState({ q }, '', `${window.location.pathname}?q=${encodeURIComponent(q)}`);
    start(q);
});
window.addEventListener('popstate', () => load());
load();

async function load() {
    const params = new URLSearchParams(window.location.search);
    const q = params.get('q')?.trim();
    if (!q) {
        status.textContent = 'Digite algo para buscar.';
        input.focus();
        return;
    }
    input.value = q;
    document.title = `${q} · Bingus`;
    if (params.has('sorte')) {
        // Estou com sorte: vai direto para o primeiro resultado.
        status.textContent = 'Procurando o melhor resultado…';
        try {
            const page = await fetchPage(q, 0, 1, false);
            if (page.results[0]) return window.location.replace(page.results[0].url);
        } catch {
            // segue para a busca normal, que mostra o erro
        }
    }
    start(q);
}

function start(q: string) {
    current = q;
    offset = 0;
    hasMore = false;
    results.innerHTML = '';
    more.textContent = '';
    timings.hidden = true;
    status.textContent = 'Buscando…';
    document.title = `${q} · Bingus`;
    search(q, 0);
}

async function fetchPage(q: string, from: number, limit: number, sums: boolean): Promise<Page> {
    const params = `q=${encodeURIComponent(q)}&limit=${limit}&offset=${from}&summaries=${sums}`;
    const r = await fetch(`${API_URL}/search?${params}`);
    if (!r.ok) {
        const body = await r.json().catch(() => null);
        throw new Error(body?.detail ?? `A API respondeu ${r.status}.`);
    }
    return (await r.json()) as Page;
}

async function search(q: string, from: number) {
    busy = true;
    if (from > 0) more.textContent = 'Carregando mais…';
    try {
        const started = performance.now();
        const page = await fetchPage(q, from, LIMIT, summariesOn());
        if (q !== current) return; // o usuário já buscou outra coisa
        if (from === 0) {
            const ms = Math.round(performance.now() - started);
            const fixed = page.corrected ? `Mostrando resultados para <b>${escape(page.corrected)}</b> · ` : '';
            status.innerHTML = page.results.length
                ? `${fixed}Resultados em ${duration(ms)}`
                : `${fixed}Nada encontrado para <b>${escape(q)}</b>. Tente outras palavras.`;
            for (const w of page.warnings) results.appendChild(note('warn', w));
            showTimings(ms, page.timings);
        }
        for (const item of page.results) results.appendChild(render(item));
        offset = from + page.results.length;
        hasMore = page.has_more;
        more.textContent = hasMore || !offset ? '' : `Fim dos resultados: ${offset}`;
    } catch (err) {
        console.error(err);
        const text = err instanceof TypeError ? `Sem resposta da API em ${API_URL}.` : (err as Error).message;
        if (from === 0) {
            status.textContent = '';
            results.replaceChildren(note('err', text));
        } else {
            more.replaceChildren(note('err', text));
        }
    } finally {
        busy = false;
    }
}

function duration(ms: number): string {
    if (ms < 1000) return `${ms} ms`;
    return `${(ms / 1000).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} segundos`;
}

function showTimings(ms: number, steps: Record<string, number>) {
    (document.getElementById('total') as HTMLElement).textContent = `${ms} ms`;
    (document.getElementById('steps') as HTMLElement).innerHTML = Object.entries(steps)
        .filter(([k]) => k !== 'total')
        .map(([k, v]) => `<div><dt>${escape(k)}</dt><dd>${Math.round(v)} ms</dd></div>`)
        .join('');
    timings.hidden = false;
}

function note(kind: 'warn' | 'err', text: string): HTMLParagraphElement {
    const p = document.createElement('p');
    p.className = `note ${kind}`;
    p.setAttribute('role', kind === 'err' ? 'alert' : 'status');
    p.textContent = text;
    return p;
}

// https://www.site.com › caminho › da › página
function crumb(url: string): string {
    try {
        const u = new URL(url);
        return [u.origin, ...u.pathname.split('/').filter(Boolean).map(decodeURIComponent)].join(' › ');
    } catch {
        return url;
    }
}

function render(item: Result): HTMLElement {
    const el = document.createElement('article');
    const host = item.host.replace(/^www\./, '');
    const meta = [
        item.bm25_rank ? `bm25 #${item.bm25_rank}` : null,
        item.vector_rank ? `vetor #${item.vector_rank}` : null,
        `rank ${Number(item.rank.toFixed(3))}`,
        item.published?.slice(0, 10),
    ].filter(Boolean).join(' · ');
    const snippet = item.snippet ? escape(item.snippet) : '';
    el.innerHTML = `
        <div class="src">
            <span class="initial" aria-hidden="true">${escape(host[0] ?? '?')}</span>
            <div>
                <span class="host">${escape(host)}</span>
                <span class="crumb">${escape(crumb(item.url))}</span>
            </div>
        </div>
        <h2><a href="${escape(item.url)}">${escape(item.title ?? host)}</a></h2>
        ${item.summary
            ? `<p class="text sum"><b>Resumo</b>${escape(item.summary)}</p>${snippet ? `<p class="text alt">${snippet}</p>` : ''}`
            : snippet ? `<p class="text nosum">${snippet}</p>` : ''}
        <div class="foot-line">
            <span class="meta mono">${meta}</span>
            <button class="lnk" type="button" aria-expanded="false">Perguntar à página</button>
        </div>
        <form class="field ask" hidden>
            <input class="q" type="text" placeholder="Pergunte algo sobre esta página" aria-label="Pergunta sobre esta página">
            <button class="cta" type="submit">Perguntar</button>
        </form>
        <p class="text answer" aria-live="polite" hidden></p>
    `;
    const toggle = el.querySelector('.foot-line button') as HTMLButtonElement;
    const ask = el.querySelector('form') as HTMLFormElement;
    const question = ask.querySelector('input') as HTMLInputElement;
    const answer = el.querySelector('.answer') as HTMLParagraphElement;
    toggle.addEventListener('click', () => {
        ask.hidden = !ask.hidden;
        toggle.setAttribute('aria-expanded', String(!ask.hidden));
        if (!ask.hidden) question.focus();
    });
    ask.addEventListener('submit', async (e) => {
        e.preventDefault();
        const prompt = question.value.trim();
        if (!prompt) return;
        answer.hidden = false;
        answer.textContent = 'Lendo a página…';
        const started = performance.now();
        try {
            const url = `${API_URL}/page?url=${encodeURIComponent(item.url)}&prompt=${encodeURIComponent(prompt)}`;
            const r = await fetch(url);
            const data = (await r.json()) as { answer: string | null; detail?: string };
            const ms = Math.round(performance.now() - started);
            answer.innerHTML = `<b>Resposta</b>${escape(data.answer ?? data.detail ?? 'sem resposta')} <span class="meta mono">${ms} ms</span>`;
        } catch (err) {
            answer.textContent = `Erro: ${err}`;
        }
    });
    return el;
}

function escape(s: string): string {
    return s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]!);
}
