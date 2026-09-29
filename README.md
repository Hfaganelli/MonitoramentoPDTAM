# Monitor de preços — Amazon e Mercado Livre

Site próprio, 100% gratuito, que acompanha o preço dos produtos que você escolher e avisa no Telegram quando baixar.

**Como funciona:** o GitHub Actions roda um robô a cada 30 minutos que lê o preço de cada produto e salva o histórico em `data/history.json`. O GitHub Pages publica o painel (`index.html`), que mostra os preços, gráficos e permite adicionar produtos.

Custo: R$ 0. Não precisa de servidor nem cartão de crédito.

---

## Instalação (cerca de 15 minutos)

### 1. Crie o repositório
1. Crie uma conta em [github.com](https://github.com) (se ainda não tiver).
2. Clique em **New repository**, dê um nome (ex.: `meus-precos`) e marque **Public**.
   Repositório público é o que libera Actions e Pages ilimitados de graça. Sua lista de produtos fica visível para quem tiver o link, mas tokens e senhas nunca ficam expostos.
3. Clique em **uploading an existing file** e envie **todos** os arquivos desta pasta, incluindo a pasta `.github` (se o navegador não mostrar pastas ocultas, arraste a pasta inteira do projeto para a página de upload).

### 2. Ligue o site (GitHub Pages)
**Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `main` / `(root)` → Save.**
Em 1–2 minutos seu site estará em `https://SEU-USUARIO.github.io/meus-precos/`.

### 3. Libere o robô para salvar dados
**Settings → Actions → General → Workflow permissions → Read and write permissions → Save.**

### 4. Crie o token para usar o painel
1. Acesse [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new).
2. **Repository access:** Only select repositories → escolha `meus-precos`.
3. **Permissions → Repository permissions:** `Contents` = Read and write, `Actions` = Read and write.
4. Gere e copie o token.
5. Abra seu site, clique em **Configurar** e cole o token. Ele fica salvo só no seu navegador.

Pronto: clique em **Adicionar produto**, cole o link, defina o preço-alvo. A primeira leitura acontece em poucos minutos.

### 5. Alertas no Telegram (opcional, recomendado)
1. No Telegram, fale com **@BotFather**, envie `/newbot` e siga as instruções. Copie o **token** do bot.
2. Mande qualquer mensagem para o seu bot novo.
3. Abra no navegador `https://api.telegram.org/botSEU_TOKEN/getUpdates` e copie o número em `"chat":{"id": ...}`.
4. No GitHub: **Settings → Secrets and variables → Actions → New repository secret** e crie:
   - `TELEGRAM_BOT_TOKEN` = token do bot
   - `TELEGRAM_CHAT_ID` = número do chat

Para testar: **Actions → Monitor de preços → Run workflow**, marque "Só enviar uma mensagem de teste no Telegram" e clique em Run workflow.

Você recebe aviso quando:
- o preço **sem cupom** fica **10% ou mais abaixo do preço normal**;
- um **cupom visível na página** deixa o preço final **20% ou mais abaixo do preço normal**;
- o preço (com ou sem cupom) chega ao **preço-alvo** que você definiu.

"Preço normal" é a mediana dos últimos 7 dias. Isso evita alarme falso por oscilação rápida e pega quedas graduais. O mesmo alerta só se repete se o preço cair pelo menos mais 1%, e volta a valer quando o preço se normaliza.

Para mudar os percentuais, crie em **Variables** (mesma tela, aba Variables) `ALERTA_QUEDA_PCT` e/ou `ALERTA_CUPOM_PCT`.

**Sobre cupons:** o robô só enxerga cupons exibidos na página do produto para qualquer visitante (ex.: "Aplicar cupom de 15%" na Amazon, "R$ 50 OFF com cupom" no Mercado Livre). Cupons da sua conta, códigos divulgados fora da página ou descontos de primeira compra não aparecem para ele. Cupons com compra mínima maior que o preço do produto são ignorados.

---

## Sobre cada loja (o que investiguei)

**Mercado Livre.** A API oficial (`api.mercadolibre.com`) passou a exigir autenticação para consultar itens. Por isso o robô lê a página do produto e extrai o preço dos dados estruturados (JSON-LD) que o próprio ML publica para o Google, o que é bem estável. Funciona com links de anúncio (`produto.mercadolivre.com.br/MLB-...`) e de catálogo (`mercadolivre.com.br/.../p/MLB...`).
Opcional: se você criar um app em [developers.mercadolivre.com.br](https://developers.mercadolivre.com.br) e gerar um token, salve como secret `ML_ACCESS_TOKEN` e o robô usa a API primeiro. Esse token expira em 6 horas, então para a maioria das pessoas a leitura da página é o caminho mais prático.

**Amazon.** A API oficial (Product Advertising API) só é liberada para afiliados que já geraram vendas, então não é uma opção gratuita para começar. O robô lê a página do produto usando `curl_cffi`, que imita o navegador Chrome e reduz bastante os bloqueios. Ainda assim, a Amazon às vezes mostra captcha para servidores de nuvem; quando isso acontece, o painel mostra "Falhou" naquele produto e o robô tenta de novo na próxima rodada. Na prática, a maioria das leituras passa.

Dica: use links limpos, como `https://www.amazon.com.br/dp/B0XXXXXXXX`. O robô também limpa os links sozinho.

---

## Usar pelo Telegram
Mande para o seu bot:
- **o link de um produto** (pode compartilhar direto do app da Amazon ou do Mercado Livre) para adicionar;
- `/add LINK 45` ou `LINK alvo 45` para adicionar já com preço-alvo; inclua `#Livros` para escolher o grupo;
- `/lista` para ver os produtos numerados;
- `/alvo 2 45`, `/grupo 2 Livros` e `/remover 2` para mudar ou remover o produto 2;
- `/ajuda` para ver os comandos.

O robô lê as mensagens a cada rodada (até 30 minutos) e responde confirmando, já com o primeiro preço. Ele só obedece ao seu Chat ID.

Além dos alertas de preço, o bot manda:
- **resumo semanal** todo domingo a partir das 9h (o que caiu, subiu, está no menor preço e tem cupom). Para testar: Actions → Run workflow → marque "Enviar o resumo semanal agora";
- **aviso de robô parado** quando um produto fica ~6 horas sem leitura, e outro quando volta a ler.

## Painel
- **Grupos:** defina no botão Editar (ou `#grupo` no Telegram). A lista é separada por grupo e cada grupo vira um filtro.
- **Ordenar:** bom momento, maior desconto, menor preço, mais recentes ou nome.
- **Termômetro:** compara o preço atual (considerando cupom) com todo o histórico: menor preço já visto, ótimo, bom, normal ou caro. Precisa de 3 dias de dados.
- **Mesmo produto em duas lojas:** ao adicionar, escolha "É o mesmo produto de outra loja?"; ou em Editar, cole o link da outra loja. O cartão mostra as duas ofertas e destaca a mais barata.
- **12 meses no Keepa:** nos produtos da Amazon, abre o histórico longo no keepa.com.

## Uso sem o painel
Você também pode editar `products.json` direto no GitHub (ícone de lápis):

```json
[
  { "url": "https://www.amazon.com.br/dp/B0XXXXXXXX", "preco_alvo": 1200, "nome": "Fone" },
  { "url": "https://produto.mercadolivre.com.br/MLB-1234567890-cadeira", "preco_alvo": 700 }
]
```

`nome` é opcional (o robô usa o título da loja). Adicione `"pausado": true` para parar de monitorar sem apagar o histórico.

## Rodar no seu computador
```bash
pip install -r requirements.txt
python scraper/monitor.py --teste "https://www.amazon.com.br/dp/B0XXXXXXXX"   # testa um link
python scraper/monitor.py                                                     # verifica todos
python -m http.server 8000   # e abra http://localhost:8000
```
Rodar em casa costuma ter menos bloqueio da Amazon do que nos servidores do GitHub, porque sua conexão é residencial. Se quiser, agende no Agendador de Tarefas do Windows ou no `cron`.

## Ajustes
- **Frequência:** em `.github/workflows/monitor.yml`, linha `cron`. `"7,37 * * * *"` = a cada 30 minutos (nos minutos 7 e 37 de cada hora). O GitHub não garante o horário exato: em picos, pode atrasar alguns minutos. Sem mudança de preço, o histórico é salvo no máximo a cada 30 minutos, para não encher o repositório.
- **Pausa automática:** o GitHub desativa agendamentos em repositórios sem atividade por 60 dias. Os commits do robô contam como atividade, mas se receber e-mail avisando, basta reativar em **Actions**.

## Problemas comuns
| Sintoma | Solução |
|---|---|
| Painel diz "Token inválido" | Gere um novo token (passo 4) e cole em Configurar. |
| Erro 403 ao adicionar produto | O token precisa de `Contents` e `Actions` com leitura e escrita neste repositório. |
| Robô roda mas não salva | Passo 3 (Read and write permissions). |
| Produto sempre "Falhou" na Amazon | Aguarde algumas rodadas; se persistir, rode no seu computador. |
| Preços não aparecem logo após adicionar | O robô leva 2–5 minutos; o painel atualiza sozinho a cada 10 minutos. |

Use com moderação e para uso pessoal: o robô espera alguns segundos entre um produto e outro para não sobrecarregar as lojas.
