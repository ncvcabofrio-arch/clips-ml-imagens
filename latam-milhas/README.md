# Monitor LATAM com milhas

Busca passagens resgatáveis com milhas LATAM Pass direto no site da LATAM, de forma agendada, e manda alerta no Telegram quando aparece voo abaixo do limite de milhas que você definiu.

**Como funciona:** um Chromium de verdade abre a página de resultados com `redemption=true` e o robô captura o JSON que o próprio site recebe da API interna (`/bff/air-offers/.../offers/search`). Tudo fica salvo em `data/milhas.db` (SQLite), o que dá histórico de preço.

> ⚠️ Não usa login. Nunca coloque sua conta LATAM Pass no robô: risco de suspensão.

## 1. Instalar na VPS (Ubuntu/Debian)

```bash
git clone <este repositório> latam-milhas   # ou copie a pasta via scp
cd latam-milhas
bash instalar.sh
```

Precisa de uns 1 a 2 GB de RAM livres para o Chromium.

## 2. Criar o bot do Telegram

1. No Telegram, fale com **@BotFather** → `/newbot` → copie o **token**.
2. Mande qualquer mensagem para o seu bot novo.
3. Abra `https://api.telegram.org/bot<TOKEN>/getUpdates` no navegador e copie o `chat.id`.
4. Coloque os dois em `config.yaml` e teste:

```bash
./rodar.sh --testar-telegram
```

## 3. Primeiro teste (importante!)

```bash
./rodar.sh --teste GIG LIS 2027-03-05
./rodar.sh --teste GRU MIA 2027-04-10 Business
```

Resultados possíveis:

| O que aparece | Significado |
|---|---|
| Lista de voos com milhas | 🎉 Funcionou. Vá para o passo 4. |
| `BLOQUEADO` / 403 | O Akamai bloqueou o IP da VPS. Veja "Se for bloqueado". |
| `0 tarifas` + arquivo `.json` em `data/debug/` | A busca passou, mas o formato do JSON é diferente do esperado. Me mande esse arquivo que ajusto o parser. |
| "Não capturei a resposta" | A página carregou, mas a API não foi chamada. Veja o `.png` em `data/debug/`. |

## 4. Agendar (cron)

`crontab -e` e adicione (roda a cada 4 horas, no minuto 17):

```
17 */4 * * * /home/SEU_USUARIO/latam-milhas/rodar.sh >> /home/SEU_USUARIO/latam-milhas/data/cron.log 2>&1
```

Cada busca (rota + dia) leva cerca de 30 a 60 s, contando a pausa. Então 2 rotas × 15 dias ≈ 20 a 30 min por varredura. Não aumente demais o volume.

## Configuração (`config.yaml`)

- `rotas`: origem, destino, intervalo de datas, cabine (`Economy`/`Business`), `max_milhas`, `max_paradas` opcional.
- `espera_entre_buscas_seg`: pausa aleatória entre buscas. Quanto maior, menor a chance de bloqueio.
- `headless: false`: usa tela virtual (xvfb). Mais difícil de ser detectado.
- `proxy`: proxy residencial opcional, caso o IP da VPS seja bloqueado.

## Se for bloqueado

1. Rode `./rodar.sh --teste ...` de novo depois de algumas horas. Às vezes o bloqueio é temporário.
2. Aumente `espera_entre_buscas_seg` e reduza a frequência do cron.
3. Use um **proxy residencial brasileiro** no campo `proxy` (pago, ex.: planos por GB).
4. Último recurso: rodar o mesmo script num computador em casa (IP residencial).

## Consultar o histórico

```bash
sqlite3 data/milhas.db "SELECT data, voo, tarifa, MIN(milhas) FROM observacoes WHERE origem='GIG' GROUP BY data, voo ORDER BY data;"
```
