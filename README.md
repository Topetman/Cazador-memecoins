# Avisos de memecoins por Telegram

Este paquete hace solo una cosa: cada 10 minutos mira lanzamientos de Solana con los filtros de Joe y, si alguno pasa, te manda un mensaje a Telegram. No compra, no vende y no firma nada. Tampoco mira X, así que el origen de la moneda no está comprobado: los avisos salen en 🟠 (o 🔴 si las cifras son justas).

## Importante antes de empezar

- Crea el repositorio **público**. En un repo público los minutos de GitHub Actions son gratis e ilimitados. En uno privado solo hay 2.000 al mes, y un barrido cada 10 minutos se los come en unos días.
- No pongas el token del bot ni el chat id en ningún archivo. Van en los Secrets de GitHub.
- El cron de GitHub no es exacto: a veces el barrido se retrasa unos minutos.
- Los avisos recuerdan los contratos ya vistos en `avisados.json`. Ese archivo se actualiza solo después de cada barrido.

## Qué necesitas

1. Una cuenta en [GitHub](https://github.com/signup) (gratis).
2. El bot de Telegram que ya tienes, creado con [@BotFather](https://t.me/BotFather), y su token.
3. Este paquete descomprimido en tu ordenador o teléfono.

## Paso 1. Crear el repositorio público

1. Entra en [github.com/new](https://github.com/new).
2. Ponle un nombre, por ejemplo `scan-memecoins`.
3. Márcalo como **Public**.
4. No marques “Add a README” ni .gitignore ni licencia: déjalo vacío.
5. Pulsa *Create repository*.

## Paso 2. Subir los archivos

1. En la página del repo vacío, pulsa *uploading an existing file* (o *Add file > Upload files*).
2. Arrastra **todos** los archivos de esta carpeta, incluida la carpeta `.github`. Esa carpeta a veces está oculta:
   - En Mac: pulsa Cmd + Mayúsculas + punto.
   - En Windows: en el Explorador, marca *Elementos ocultos*.
3. Si no consigues subir `.github`, en el repo pulsa *Add file > Create new file*, escribe exactamente `.github/workflows/scan.yml` y pega el contenido de esa misma ruta. Repite con `.github/workflows/get_chat_id.yml`.
4. Pulsa *Commit changes*.

## Paso 3. Escribirle al bot

Abre tu bot en Telegram y mándale un mensaje, por ejemplo `hola`. Así GitHub podrá leer tu chat id. Si no le escribes antes, el siguiente paso no verá nada.

## Paso 4. Guardar el token y sacar el chat id

1. En el repo, entra en *Settings > Secrets and variables > Actions*.
2. Pulsa *New repository secret*.
3. Nombre: `TELEGRAM_BOT_TOKEN`. Valor: el token que te dio BotFather. Pulsa *Add secret*.
4. Arriba, entra en la pestaña *Actions*. Si GitHub te pide confirmar que confías en los workflows, acéptalo.
5. En la lista de la izquierda, elige **Obtener chat id** y pulsa *Run workflow*.
6. Cuando termine, abre esa ejecución, pulsa el paso *Leer mensajes y mostrar el chat id* y copia el número que aparece (`id=123456789`).
7. Vuelve a *Settings > Secrets and variables > Actions*, crea otro secret llamado `TELEGRAM_CHAT_ID` y pega ese número.

## Paso 5. Probar el barrido a mano

1. En *Actions*, elige **Scan memecoins** y pulsa *Run workflow*.
2. Tarda unos 4 minutos, a veces un poco más si GeckoTerminal va justo de ritmo. Si hay un candidato que pase los filtros y no esté ya avisado, te llegará un mensaje a Telegram. Si no hay ninguno, no pasa nada: el workflow termina igual.
3. A partir de ahí se lanzará solo, más o menos cada 10 minutos.

## Qué hace y qué no hace (frente a Joe)

Lo que sí hace:

- Mira lanzamientos recientes y monedas “en carrera” (1–24 h, 100K–3M $) en Dexscreener, GeckoTerminal, Raydium LaunchLab y Bags, y comprueba el contrato en Rugcheck. No usa PumpPortal (ese va por websocket y alargaría el barrido).
- Aplica los filtros: ratios por tramo, volumen, máximo 24 h, LP bloqueado, líder del enjambre (al menos el doble de mcap que la siguiente copia válida), y los descartes duros (wash, bundle, LP desbloqueado, liquidez mayor que el mcap, liquidez drenada, una wallet con más del 25 %).
- Recuerda los contratos ya avisados para no repetirlos.
- Te manda el análisis en español, el contrato en su propia línea y el enlace de Dexscreener.

Lo que no hace (eso sigue siendo cosa de Joe, con un «5» o un «6»):

- No lee X, así que no comprueba la narrativa ni el origen. Por eso nunca avisa en 🟢.
- No hace el análisis a fondo de un contrato concreto ni te escribe comentarios para pump.fun.
- No puede firmar, conectar una wallet ni probar un juego.

## Si algo falla

- El workflow *Obtener chat id* no ve mensajes: escribe otra vez `hola` al bot y vuelve a ejecutarlo.
- El scan termina en error por Telegram: revisa que los dos secrets existan y que los nombres sean exactamente `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.
- Quieres pausarlo: en *Actions*, entra en **Scan memecoins**, pulsa los tres puntos y elige *Disable workflow*.
