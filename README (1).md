# SD EOT Exam — Corrector

App Flask de un solo archivo (`app.py`) que corrige los intentos del
formulario de Google **"SD EOT Exam"**: marca preguntas falladas, decide
Aprobado/Suspendido y genera automáticamente el mensaje final (idéntico a
`formatfailed.txt` / `formatpassed.txt`) para copiar o descargar.

Puede correr **en local, en tu propio PC**, o desplegarse en
**[Render](https://render.com)** para tener una URL fija a la que entrar
desde cualquier sitio. Las instrucciones de local están en las secciones 1-3;
las de Render, en la sección 4.

## 1. Configurar Google Cloud (una sola vez)

La app necesita su propia credencial OAuth para leer el formulario y sus
respuestas. En [console.cloud.google.com](https://console.cloud.google.com):

1. Crea un proyecto (o usa uno que ya tengas, p. ej. el del portal BARC COMPANY).
2. En **APIs y servicios → Biblioteca**, habilita:
   - **Google Drive API**
   - **Google Forms API**
3. En **APIs y servicios → Pantalla de consentimiento OAuth**:
   - Tipo "Externo", modo **Prueba** (no hace falta publicarla).
   - Añade los scopes: `.../auth/drive.metadata.readonly`,
     `.../auth/forms.body.readonly`, `.../auth/forms.responses.readonly`,
     `.../auth/userinfo.email`.
   - Añade tu propio correo como **usuario de prueba**.
4. En **Credenciales → Crear credenciales → ID de cliente de OAuth**:
   - Tipo: **Aplicación web**.
   - En "URI de redireccionamiento autorizados" añade:
     `http://localhost:5000/oauth2callback` (para uso local).
   - Si además vas a desplegar en Render (sección 4), puedes añadir **ya
     mismo o más adelante** una segunda URI con tu dominio de Render, p. ej.
     `https://sd-eot-exam.onrender.com/oauth2callback` — el mismo cliente
     OAuth sirve para ambos entornos, solo hace falta que la URI exacta que
     use la app en cada sitio esté en esta lista.
   - Copia el **Client ID** y el **Client secret**.

⚠️ En modo "Prueba" los tokens caducan a los 7 días — pasado ese tiempo solo
tienes que volver a iniciar sesión en la app, nada más.

## 2. Instalar y configurar

```bash
pip install -r requirements.txt
```

Copia `.env.example` como `.env` (misma carpeta que `app.py`) y rellena:

```
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://localhost:5000/oauth2callback
FLASK_SECRET_KEY=cualquier-cosa-larga-y-aleatoria
ADMIN_EMAIL=tucorreo@gmail.com
FORM_TITLE=SD EOT Exam
```

La app carga el `.env` sola al arrancar.

## 3. Arrancar

```bash
python app.py
```

Abre **http://localhost:5000** en el navegador e inicia sesión con tu
cuenta de Google (la que pusiste en `ADMIN_EMAIL`).

La primera vez, la app crea sola un archivo `eot_ledger.db` (SQLite) en la
misma carpeta — ahí guarda qué exámenes ya se corrigieron. Bórralo solo si
quieres resetear el historial de correcciones.

## 4. Desplegar en Render (opcional)

El repo ya incluye lo necesario: `Procfile`, `render.yaml` y `gunicorn` en
`requirements.txt`. `app.py` detecta que corre en Render (variable
`RENDER_EXTERNAL_URL`, que Render define sola) y ajusta automáticamente el
host/puerto, la redirect URI de OAuth y las cookies seguras — no hace falta
tocar código.

### 4.1 Opción rápida: Blueprint

1. Sube este repo a GitHub (o GitLab).
2. En el dashboard de Render: **New +** → **Blueprint**, y selecciona el
   repo. Render leerá `render.yaml` y creará el servicio con el plan
   **Starter** (de pago, ~7 $/mes) más un disco persistente de 1 GB.
3. Render te pedirá rellenar las variables marcadas como secretas:
   `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ADMIN_EMAIL`. El resto
   (`FLASK_SECRET_KEY`, `DB_PATH`, `FORM_TITLE`) ya vienen definidas.
4. Despliega. Cuando termine, copia la URL pública que te asigna Render
   (algo como `https://sd-eot-exam.onrender.com`).
5. Vuelve a Google Cloud Console → Credenciales → tu cliente OAuth, y añade
   `https://TU-URL-DE-RENDER.onrender.com/oauth2callback` a las URIs de
   redireccionamiento autorizadas (si no lo hiciste ya en el paso 1.4).
   No hace falta volver a desplegar nada: la app calcula esa misma URI sola
   en cuanto la lees en las variables de entorno de Render.
6. Abre la URL de Render e inicia sesión con la cuenta de `ADMIN_EMAIL`.

### 4.2 Opción manual (sin Blueprint)

Si prefieres no usar `render.yaml`: crea un **Web Service** apuntando al
repo, con:

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn -w 1 --threads 4 --timeout 120 app:app`
- Variables de entorno: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  `ADMIN_EMAIL`, `FLASK_SECRET_KEY` (algo largo y aleatorio, fijo — no lo
  regeneres en cada deploy) y opcionalmente `FORM_TITLE`.
- El **puerto** no hay que configurarlo: Render inyecta `PORT` solo y
  `app.py` ya lo lee.

### 4.3 Cosas a tener en cuenta en Render

- **`-w 1` es obligatorio.** Las sesiones de login viven en memoria del
  proceso (`SESSIONS`, pensado para un único usuario). Con más de un worker
  gunicorn, la mitad de las peticiones caerían en un proceso que no tiene tu
  sesión y te desconectaría al azar.
- **Filesystem efímero salvo disco persistente.** Render borra los cambios
  en disco (incluido `eot_ledger.db`) en cada redeploy y cada vez que una
  instancia gratuita se "duerme" y vuelve a arrancar. El `render.yaml`
  incluido usa un plan de pago con un disco persistente montado en
  `/var/data`, y `DB_PATH=/var/data/eot_ledger.db` para que el registro de
  exámenes corregidos sobreviva. Si usas el **plan Free** (borrando el
  bloque `disk`), la app funciona igual, pero el historial de "ya
  corregidos" se resetea de vez en cuando y algún examen ya corregido podría
  volver a aparecer como pendiente.
- **El plan Free se "duerme".** Tras ~15 min sin tráfico, Render para la
  instancia; la siguiente visita tarda ~1 minuto en despertarla, y de paso
  se pierde la sesión de login (tendrás que volver a iniciar sesión). El
  plan Starter no tiene este problema.
- Los tokens de Google en modo "Prueba" siguen caducando a los 7 días,
  igual que en local — solo hay que volver a iniciar sesión.

## Cómo funciona

- Al entrar, la app busca en tu Drive un Formulario de Google llamado
  exactamente **"SD EOT Exam"** (o el que pongas en `FORM_TITLE`) y lee sus
  respuestas.
- **Pendientes**: respuestas que aún no están en el registro local.
- Seleccionas una, marcas (con un clic) las preguntas que el usuario falló.
  La pregunta con "usuario"/"username"/"discord"/"roblox" en el título se usa
  automáticamente como nombre del alumno y no se muestra como pregunta a corregir.
- El botón flotante **"Finalizar corrección"** guarda el estado tal cual esté
  (las preguntas sin marcar quedan como correctas), pide confirmar, y luego
  pregunta Aprobado/Suspendido.
- Según la respuesta, genera el mensaje con la plantilla correspondiente,
  sustituyendo el nombre de usuario y la lista de preguntas falladas. Puedes
  copiarlo o descargarlo como `.txt`.
- El examen pasa a **"Corregidos recientemente"**, con un botón para
  eliminarlo manualmente. Si no lo eliminas, desaparece solo de esa lista
  pasadas **2 horas** (pero no vuelve a aparecer como pendiente — Google
  Forms no permite borrar respuestas reales vía API, así que "eliminar"
  solo oculta en el registro local, nunca toca el Google Form).

## Limitaciones a tener en cuenta

- Solo se procesan preguntas de texto/opción simple (respuestas de tipo
  cuadrícula o subida de archivo no están soportadas).
- Pensada para que la uses tú solo (`ADMIN_EMAIL`); si algún día quieres que
  varias personas corrijan a la vez, habría que ampliar el modelo de sesión.
- Estas mismas dos características (sesión en memoria de un solo proceso,
  `ADMIN_EMAIL` único) significan que no se debe correr con más de un
  worker/instancia a la vez, ni en local ni en Render — ver sección 4.3.
