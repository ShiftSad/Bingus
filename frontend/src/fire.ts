// Fogo em ASCII atrás da busca da página inicial: ruído de Perlin subindo da base da tela. O cursor
// atiça o fogo em volta dele, o campo em foco acende uma bolha atrás da barra e cada tecla solta uma onda.

const COLOR = [255, 59, 47];
const RAMP = ['·', ':', '-', '=', '+', '*', '#', '%'];
const TH = [0.42, 0.48, 0.54, 0.6, 0.66, 0.72, 0.78, 0.85];
const ALPHA = [0.22, 0.32, 0.44, 0.58, 0.72, 0.86, 1, 1];
const CW = 10;
const CH = 18;

// Tabela de permutação embaralhada com semente fixa, duplicada para não estourar o índice.
const P = (() => {
    const p: number[] = [];
    let s = 1337;
    for (let i = 0; i < 256; i++) p[i] = i;
    for (let i = 255; i > 0; i--) {
        s = (s * 16807) % 2147483647;
        const j = s % (i + 1);
        [p[i], p[j]] = [p[j], p[i]];
    }
    return p.concat(p);
})();

function grad(h: number, a: number, b: number, c: number) {
    h &= 15;
    const u = h < 8 ? a : b;
    const v = h < 4 ? b : h === 12 || h === 14 ? a : c;
    return (h & 1 ? -u : u) + (h & 2 ? -v : v);
}

// Perlin clássico em 3D, mais ou menos de -1 a 1.
function noise(x: number, y: number, z: number) {
    const fx = Math.floor(x);
    const fy = Math.floor(y);
    const fz = Math.floor(z);
    const X = fx & 255;
    const Y = fy & 255;
    const Z = fz & 255;
    x -= fx;
    y -= fy;
    z -= fz;
    const u = x * x * x * (x * (x * 6 - 15) + 10);
    const v = y * y * y * (y * (y * 6 - 15) + 10);
    const w = z * z * z * (z * (z * 6 - 15) + 10);
    const A = P[X] + Y;
    const AA = P[A] + Z;
    const AB = P[A + 1] + Z;
    const B = P[X + 1] + Y;
    const BA = P[B] + Z;
    const BB = P[B + 1] + Z;
    const x1 = grad(P[AA], x, y, z);
    const x2 = grad(P[BA], x - 1, y, z);
    const x3 = grad(P[AB], x, y - 1, z);
    const x4 = grad(P[BB], x - 1, y - 1, z);
    const x5 = grad(P[AA + 1], x, y, z - 1);
    const x6 = grad(P[BA + 1], x - 1, y, z - 1);
    const x7 = grad(P[AB + 1], x, y - 1, z - 1);
    const x8 = grad(P[BB + 1], x - 1, y - 1, z - 1);
    const a1 = x1 + u * (x2 - x1);
    const a2 = x3 + u * (x4 - x3);
    const a3 = x5 + u * (x6 - x5);
    const a4 = x7 + u * (x8 - x7);
    const b1 = a1 + v * (a2 - a1);
    const b2 = a3 + v * (a4 - a3);
    return b1 + w * (b2 - b1);
}

// A cor base nas bordas da chama, clareando no miolo.
const tint = (k: number) => `rgb(${COLOR.map((c) => Math.round(c + (255 - c) * k)).join(',')})`;
const PAL = [0, 0, 0, 0, 0, 0, 0.22, 0.5].map(tint);

export function fire(canvas: HTMLCanvasElement, field: HTMLElement, input: HTMLInputElement) {
    const mouse = { x: 0, y: 0, on: false };
    const cur = { x: 0, y: 0, r: 0 };
    let focused = false;
    let focusR = 0;
    let ripples: { x: number; y: number; t0: number }[] = [];
    let last = 0;
    let t = 0;
    let now = 0;

    const local = (x: number, y: number) => {
        const r = canvas.getBoundingClientRect();
        return { x: x - r.left, y: y - r.top };
    };
    const barCenter = () => {
        const r = field.getBoundingClientRect();
        return local(r.left + r.width / 2, r.top + r.height / 2);
    };

    document.addEventListener('mousemove', (e) => {
        const p = local(e.clientX, e.clientY);
        mouse.x = p.x;
        mouse.y = p.y;
        if (!mouse.on) Object.assign(cur, p);
        mouse.on = true;
    });
    document.documentElement.addEventListener('mouseleave', () => (mouse.on = false));
    input.addEventListener('input', () => {
        ripples.push({ ...barCenter(), t0: now });
        if (ripples.length > 6) ripples.shift();
    });
    input.addEventListener('focus', () => (focused = true));
    input.addEventListener('blur', () => (focused = false));

    function draw(dt: number) {
        const W = canvas.clientWidth;
        const H = canvas.clientHeight;
        if (!W || !H) return;
        const dpr = Math.min(2, window.devicePixelRatio || 1);
        const pw = Math.round(W * dpr);
        const ph = Math.round(H * dpr);
        if (canvas.width !== pw || canvas.height !== ph) {
            canvas.width = pw;
            canvas.height = ph;
        }
        const ctx = canvas.getContext('2d')!;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, W, H);
        ctx.font = "500 13px 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace";
        ctx.textBaseline = 'middle';
        ctx.textAlign = 'center';

        // x e y são a tela, z é o tempo; o ruído rola para cima e a intensidade cai com a altura.
        const z = t * 0.35;
        const rise = t * 90;
        const blobs: number[] = [];
        const ease = (k: number) => Math.min(1, dt * k);
        cur.r += ((mouse.on ? 95 : 0) - cur.r) * ease(6);
        cur.x += (mouse.x - cur.x) * ease(8);
        cur.y += (mouse.y - cur.y) * ease(8);
        if (cur.r > 1) blobs.push(cur.x, cur.y, cur.r * cur.r);
        focusR += ((focused ? 130 : 0) - focusR) * ease(4);
        if (focusR > 1) {
            const p = barCenter();
            blobs.push(p.x, p.y, focusR * focusR);
        }
        ripples = ripples.filter((r) => now - r.t0 < 2.5);
        const waves = ripples.map((r) => {
            const age = now - r.t0;
            return [r.x, r.y, age * 260, 0.35 * Math.exp(-age * 1.6)];
        });

        const buckets: number[][] = [[], [], [], [], [], [], [], []];
        const cols = Math.ceil(W / CW);
        const rows = Math.ceil(H / CH);
        const cx0 = W / 2;
        const cy0 = H * 0.46;
        const rx = W * 0.3;
        const ry = H * 0.26;
        for (let j = 0; j < rows; j++) {
            const y = j * CH + CH / 2;
            for (let i = 0; i < cols; i++) {
                const x = i * CW + CW / 2;
                const nx = x * 0.0048;
                const ny = (y + rise) * 0.0036;
                const wx = nx + 0.9 * noise(nx * 0.7 + 3.1, ny * 0.7, z);
                const f =
                    (noise(wx, ny, z) +
                        0.5 * noise(wx * 2 + 17, ny * 2, z * 1.5 + 9) +
                        0.25 * noise(wx * 4, ny * 4 + 31, z * 2 + 21)) /
                    1.75;
                let v = Math.pow(y / H, 1.4) * 1.1 + f * 0.95 - 0.12;
                for (let k = 0; k < blobs.length; k += 3) {
                    const dx = x - blobs[k];
                    const dy = y - blobs[k + 1];
                    v += 0.4 * Math.exp(-(dx * dx + dy * dy) / (2 * blobs[k + 2]));
                }
                for (const [wx0, wy0, radius, amp] of waves) {
                    const dx = x - wx0;
                    const dy = y - wy0;
                    const d = Math.sqrt(dx * dx + dy * dy) - radius;
                    v += amp * Math.exp(-(d * d) / 1352);
                }
                if (v < TH[0]) continue;
                let l = 0;
                while (l < 7 && v >= TH[l + 1]) l++;
                // Apaga um pouco no centro para o logo e a barra continuarem legíveis.
                const ex = (x - cx0) / rx;
                const ey = (y - cy0) / ry;
                buckets[l].push(x, y, Math.max(0.3, Math.min(1, ex * ex + ey * ey)));
            }
        }
        for (let l = 0; l < 8; l++) {
            const b = buckets[l];
            ctx.fillStyle = PAL[l];
            for (let k = 0; k < b.length; k += 3) {
                ctx.globalAlpha = ALPHA[l] * b[k + 2];
                ctx.fillText(RAMP[l], b[k], b[k + 1]);
            }
        }
        ctx.globalAlpha = 1;
    }

    function frame(ms: number) {
        requestAnimationFrame(frame);
        if (document.hidden) {
            last = ms;
            return;
        }
        const dt = Math.min(0.1, (ms - last) / 1000);
        if (dt < 0.04) return; // uns 25 quadros por segundo bastam
        last = ms;
        t += dt;
        now += dt;
        draw(dt);
    }

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        t = 7;
        document.fonts.ready.then(() => draw(0));
        window.addEventListener('resize', () => draw(0));
        return;
    }
    requestAnimationFrame(frame);
}
