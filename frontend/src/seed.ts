/// <reference types="vite/client" />
const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';

interface Progress {
    started_at: string;
    updated_at: string;
    done: number;
    skipped: number;
    rate: number;
    bytes_read: number;
    bytes_total: number;
    finished: boolean;
    page_count: number | null;
    pt_count: number | null;
    chunks_pending: number;
    eta_s: number | null;
}

interface Recent {
    url: string;
    title: string | null;
    chars: number | null;
    fetched_at: string;
}

const status = document.getElementById('status')!;
const bar = document.getElementById('bar')!;
const cards = document.getElementById('cards')!;
const recent = document.getElementById('recent')!;

async function refresh() {
    try {
        const r = await fetch(`${API_URL}/seed/status`);
        const data = (await r.json()) as { progress: Progress | null; recent: Recent[] };
        render(data.progress, data.recent);
    } catch (err) {
        status.textContent = `Sem contato com a API em ${API_URL}: ${err}`;
    }
}

function render(p: Progress | null, list: Recent[]) {
    if (!p) {
        status.textContent = 'Nenhuma importação registrada ainda.';
        return;
    }
    const pct = p.bytes_total ? (100 * p.bytes_read) / p.bytes_total : 0;
    bar.style.width = `${pct.toFixed(1)}%`;
    bar.textContent = `${pct.toFixed(1)}%`;
    bar.classList.toggle('progress-bar-animated', !p.finished);
    const stale = (Date.now() - new Date(p.updated_at).getTime()) / 1000;
    status.textContent = p.finished
        ? `Concluída. Começou em ${fmtDate(p.started_at)}, terminou em ${fmtDate(p.updated_at)}.`
        : stale > 60
          ? `Parada? Última atualização há ${fmtDuration(stale)}.`
          : `Em andamento desde ${fmtDate(p.started_at)}, atualizado há ${Math.round(stale)} s.`;
    card('Artigos importados', p.done.toLocaleString('pt-BR'));
    card('Pulados', p.skipped.toLocaleString('pt-BR'), 'redirects e vazios');
    card('Velocidade', `${p.rate.toFixed(0)} / s`);
    card('Tempo restante', p.eta_s == null ? '—' : fmtDuration(p.eta_s));
    card('Dump lido', `${gb(p.bytes_read)} de ${gb(p.bytes_total)} GB`);
    card('Páginas da Wikipédia no índice', (p.page_count ?? 0).toLocaleString('pt-BR'));
    card('Chunks esperando embedding', p.chunks_pending.toLocaleString('pt-BR'));
    recent.innerHTML = list
        .map(
            (a) => `<tr>
                <td>${escape(a.title ?? '')}</td>
                <td><a href="${a.url}" target="_blank">${escape(decodeURIComponent(a.url).replace('https://pt.wikipedia.org/wiki/', ''))}</a></td>
                <td class="text-end">${(a.chars ?? 0).toLocaleString('pt-BR')}</td>
                <td>${fmtDate(a.fetched_at)}</td>
            </tr>`,
        )
        .join('');
}

const seen = new Map<string, HTMLElement>();
function card(label: string, value: string, hint = '') {
    let el = seen.get(label);
    if (!el) {
        el = document.createElement('div');
        el.className = 'col-6 col-md-3';
        cards.appendChild(el);
        seen.set(label, el);
    }
    el.innerHTML = `<div class="card h-100"><div class="card-body py-2">
        <div class="text-muted small">${label}</div>
        <div class="fs-4">${value}</div>
        <div class="text-muted small">${hint}</div>
    </div></div>`;
}

function gb(n: number) {
    return (n / 1e9).toFixed(2);
}
function fmtDate(s: string) {
    return new Date(s).toLocaleString('pt-BR');
}
function fmtDuration(s: number) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h ? `${h} h ${m} min` : m ? `${m} min` : `${Math.round(s)} s`;
}
function escape(s: string): string {
    return s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]!);
}

refresh();
setInterval(refresh, 5000);
