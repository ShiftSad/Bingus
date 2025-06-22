# Bingus

Uma engine de pesquisa feita do zero com postgres

## Experimente Live agora!
[bingus.shiftsad.dev](https://bingus.shiftsad.dev)

![Página Inicial](https://3s6cswpu71.ufs.sh/f/37o8Wzy4EnVtf0NIRLOxvZ68kj9G4zuJHTlRAXUwdKtWSrmI)
![Página de Pesquisa](https://3s6cswpu71.ufs.sh/f/37o8Wzy4EnVtNR1VCDoC4Ojw7UL9IqcK2euMb0PsyzASERYo)

## Como funciona?
O sistema realizou o *scrapping* de 800 mil páginas do Google e coletou 61 milhões de links de *backreference* para o ranqueamento das páginas.

![Schema do Banco de Dados](https://3s6cswpu71.ufs.sh/f/37o8Wzy4EnVtwJVy6oGmOVTZvJlioGEInyzmUDH19L5c0NX3)

Um script SQL é utilizado para iterar 20 vezes por todos os links, aumentando a pontuação dos links mais frequentemente referenciados. Essas 20 iterações são realizadas porque, se um link é chamado a partir de uma fonte de maior qualidade (com melhor ranqueamento), ele recebe pontos extras. Embora 20 passes possam parecer um exagero para garantir maior precisão, é uma medida que achei interessante para o projeto.

Na coluna de todas as páginas, durante o crawling, todo o conteúdo da página foi capturado e salvo em uma tabela. Em seguida, foi feito o *embedding* desse conteúdo usando o modelo `all-mpnet-base-v2` para possibilitar uma pesquisa mais inteligente. Assim, para cada *prompt* de pesquisa, o *backend* invoca o mesmo modelo via `@xenova/transformers` e realiza a busca utilizando o `pgvector`.

## Techstack

*   **Backend:** TypeScript (NestJS)
*   **Frontend:** Vite, TypeScript
*   **Banco de Dados:** PostgreSQL (PL/pgSQL)
*   **Containerização:** Docker
*   **Scrapper:** Python

## Por essa escolha?

### NestJS
NestJs realmente chamou minha atenção por ser algo extra do que eu estou acostumado, lidando com servidores TCP diretamente. Decidi experimentar com uma experiência mais opnionada, e desbravar o outro lado da força.

### Vite
Com o Nest, já estava muito fora da minha zona de conforto, tive que voltar pelo menos um pouco.

### Crawler
Minha primeira vez fazendo algo do tipo, mas sabia que Python seria minha melhor alternativa. Iniciei seguindo uma matéria do Medium explicando o básico de Crawlers, e continuei dai. Tive que experimentar e me confortar com asyncio, que era uma coisa que nunca tinha usado fortemente antes.

### Database
Já tinha experiência com PgVector, logo, fui no fácil. Não via necessidade de buscar algo especializado, quando eu já ia precisar de qualquer forma usar um banco de dados convencional. Pode até ser menos eficiência, mas por ser um projeto, imagino que essa perda tenha sido obfuscada por um serviço a menos.

## Contribuições
Prefiro que não, esse foi um projeto de uma madrugada com objetivo de aprendizado, o código pode melhor muito ainda, e não foi feito com a mentalidade de ser mantido.
