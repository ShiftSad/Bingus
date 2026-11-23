/// <reference types="vite/client" />
const API_URL = (import.meta.env.VITE_API_URL ?? 'http://localhost:8000').replace(/\/$/, '');
const MCP_URL = `${API_URL}/mcp`;

// Os exemplos trazem MCP_URL e API_URL no lugar do endereço da instância.
const fill = (s: string) => s.replace(/MCP_URL/g, MCP_URL).replace(/API_URL/g, API_URL);
for (const el of document.querySelectorAll<HTMLElement>('[data-url]')) el.textContent = fill(el.textContent ?? '');

async function copy(button: HTMLButtonElement, text: string) {
    await navigator.clipboard.writeText(text);
    const label = button.textContent;
    button.textContent = 'Copiado';
    setTimeout(() => (button.textContent = label), 1500);
}
for (const b of document.querySelectorAll<HTMLButtonElement>('[data-copy]')) {
    b.addEventListener('click', () => copy(b, fill(b.dataset.copy!)));
}
for (const b of document.querySelectorAll<HTMLButtonElement>('.copy')) {
    b.addEventListener('click', () => copy(b, b.parentElement!.querySelector('pre')!.textContent ?? ''));
}

// Abas dos clientes, com setas do teclado como pede o padrão de tablist.
const tabs = [...document.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
function select(tab: HTMLButtonElement) {
    for (const t of tabs) {
        const on = t === tab;
        t.setAttribute('aria-selected', String(on));
        t.tabIndex = on ? 0 : -1;
        document.getElementById(t.getAttribute('aria-controls')!)!.hidden = !on;
    }
}
tabs.forEach((tab, i) => {
    tab.addEventListener('click', () => select(tab));
    tab.addEventListener('keydown', (e) => {
        const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
        if (!step) return;
        const next = tabs[(i + step + tabs.length) % tabs.length];
        select(next);
        next.focus();
    });
});
