import { fire } from './fire';
import { voice } from './voice';

const form = document.getElementById('search-form') as HTMLFormElement;
const input = document.getElementById('q') as HTMLInputElement;
voice(document.getElementById('mic') as HTMLButtonElement, input, () => form.requestSubmit());
fire(document.getElementById('fire') as HTMLCanvasElement, document.getElementById('field') as HTMLElement, input);
