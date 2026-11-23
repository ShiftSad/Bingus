// Busca por voz com a Web Speech API. Sem suporte no navegador (Firefox), o botão fica escondido.
export function voice(button: HTMLButtonElement, input: HTMLInputElement, done: () => void) {
    const w = window as any;
    const Recognition = w.SpeechRecognition ?? w.webkitSpeechRecognition;
    if (!Recognition) return;
    button.hidden = false;
    button.addEventListener('click', () => {
        const rec = new Recognition();
        rec.lang = 'pt-BR';
        rec.onresult = (e: any) => {
            input.value = e.results[0][0].transcript;
            done();
        };
        rec.onend = () => button.classList.remove('on');
        button.classList.add('on');
        rec.start();
    });
}
