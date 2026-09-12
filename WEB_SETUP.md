# News Pulse - Configuración OAuth de Google

Esta guía explica cómo configurar, ejecutar y comprobar la integración
OAuth de Google utilizada por News Pulse.

Para la instalación general y la descripción completa del proyecto,
consulta `README.md`.

## Arquitectura actual

News Pulse es actualmente una aplicación monousuario.

El flujo de autenticación es:

```text
Navegador
    ↓
GET /auth/google
    ↓
Google OAuth
    ↓
GET /auth/google/callback
    ↓
validación y consumo de oauth_state
    ↓
credenciales OAuth cifradas con Fernet
    ↓
SQLite
    ↓
sesión firmada con account_id
    ↓
Gmail API
```

Las credenciales OAuth completas nunca se almacenan en la cookie del
navegador.

La sesión Flask contiene únicamente:

- `oauth_state` mientras se completa OAuth.
- `account_id` después de autenticar la cuenta.

El valor permitido actualmente para `account_id` es el identificador
interno monousuario definido por `OAuthCredentialStore`.

## Requisitos de Google Cloud

Se necesita:

- Un proyecto de Google Cloud.
- La Gmail API habilitada.
- Una pantalla de consentimiento OAuth configurada.
- Un cliente OAuth de tipo aplicación web.
- La cuenta autorizada como usuario de prueba mientras la aplicación
  permanezca en modo de pruebas.

En el cliente OAuth configura:

```text
Authorized JavaScript origin:
http://localhost:5000

Authorized redirect URI:
http://localhost:5000/auth/google/callback
```

El origen y la URI deben coincidir exactamente con la configuración de
News Pulse.

Durante el flujo OAuth utiliza siempre:

```text
http://localhost:5000
```

No alternes entre `localhost` y `127.0.0.1`, porque el navegador los
considera hosts diferentes y no comparte las cookies entre ellos.

## Credenciales de la aplicación web

Descarga el JSON del cliente OAuth de tipo aplicación web y guárdalo
fuera del control de versiones.

Una ubicación local recomendada es:

```text
secrets/google_oauth_web.json
```

Protege el archivo para que solamente pueda leerlo tu usuario:

```bash
chmod 600 secrets/google_oauth_web.json
```

No publiques este JSON ni copies su contenido en documentación, commits
o capturas de pantalla.

## Variables de entorno

La configuración se carga desde el archivo `.env` situado en la raíz
del proyecto.

Las variables relacionadas con OAuth son:

```ini
GOOGLE_APPLICATION_CREDENTIALS_WEB_APP=secrets/google_oauth_web.json
GOOGLE_REDIRECT_URI=http://localhost:5000/auth/google/callback
GOOGLE_SCOPES=https://www.googleapis.com/auth/gmail.readonly

FLASK_SECRET_KEY_PATH=secrets/flask_secret.key
TOKEN_ENCRYPTION_KEY_PATH=secrets/token_encryption.key

DATABASE_PATH=data/newspulse.db
```

No añadas valores reales a `.env.example`.

El scope utilizado actualmente permite solamente leer Gmail:

```text
https://www.googleapis.com/auth/gmail.readonly
```

El envío del correo maestro requerirá añadir posteriormente:

```text
https://www.googleapis.com/auth/gmail.send
```

Cuando se modifiquen los scopes será necesario volver a autorizar la
cuenta de Google.

## Claves locales

News Pulse utiliza dos claves diferentes:

- `FLASK_SECRET_KEY_PATH`: firma la cookie de sesión Flask.
- `TOKEN_ENCRYPTION_KEY_PATH`: cifra las credenciales OAuth almacenadas
  en SQLite.

Para una instalación nueva pueden generarse así:

```bash
umask 077

myvenv/bin/python -c \
  "import secrets; print(secrets.token_hex(32))" \
  > secrets/flask_secret.key

myvenv/bin/python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  > secrets/token_encryption.key
```

Después, limita sus permisos:

```bash
chmod 600 \
  secrets/flask_secret.key \
  secrets/token_encryption.key
```

No reemplaces `token_encryption.key` si la base de datos ya contiene
credenciales OAuth. Una clave Fernet distinta no podrá descifrar los
datos existentes.

Cambiar `flask_secret.key` invalida las sesiones abiertas en los
navegadores.

## Persistencia de credenciales

Después de completar OAuth:

1. Google entrega las credenciales al callback.
2. News Pulse consulta el perfil Gmail.
3. `OAuthCredentialStore` cifra las credenciales completas con Fernet.
4. Las credenciales cifradas se guardan en SQLite.
5. La sesión anterior se elimina.
6. La nueva sesión conserva únicamente `account_id`.

La ubicación predeterminada de la base de datos es:

```text
data/newspulse.db
```

La base de datos no debe subirse a Git.

El almacenamiento monousuario impide utilizar identificadores de cuenta
arbitrarios. Si la sesión contiene un identificador no permitido, el
backend rechaza la sesión y la elimina.

## Renovación de credenciales

Antes de acceder a Gmail, News Pulse comprueba las credenciales
almacenadas.

Puede ocurrir uno de estos casos:

- Si siguen siendo válidas, se utilizan directamente.
- Si han caducado y existe un refresh token, se renuevan y se vuelven a
  guardar cifradas.
- Si falta el refresh token o Google revoca la autorización, la cuenta
  se marca como revocada y se solicita una nueva autenticación.
- Si Google no está disponible temporalmente, el backend conserva las
  credenciales y permite reintentar más tarde.

## Ejecutar la aplicación

Activa el entorno virtual si todavía no está activo:

```bash
source myvenv/bin/activate
```

Después ejecuta:

```bash
python run_web_app.py
```

También puedes ejecutarla directamente con el intérprete del entorno:

```bash
myvenv/bin/python run_web_app.py
```

Abre en el navegador:

```text
http://localhost:5000
```

El servidor incorporado de Flask es exclusivamente para desarrollo.

## Endpoints OAuth y Gmail

| Método | Ruta | Función |
|---|---|---|
| `GET` | `/` | Muestra la página de inicio. |
| `GET` | `/auth/google` | Inicia el proceso OAuth. |
| `GET` | `/auth/google/callback` | Valida la respuesta de Google. |
| `GET` | `/dashboard` | Muestra el dashboard autenticado. |
| `GET` | `/api/gmail/profile` | Obtiene el perfil Gmail. |
| `GET` | `/api/gmail/messages` | Lista mensajes recientes. |
| `POST` | `/logout` | Elimina la sesión local. |

El logout solamente elimina la sesión del navegador.

Actualmente no:

- Revoca la autorización concedida en Google.
- Elimina las credenciales cifradas de SQLite.
- Desconecta permanentemente la cuenta.

Esa operación deberá implementarse mediante una acción de desconexión
explícita si se necesita más adelante.

## Comprobación manual

### 1. Autenticación

1. Abre `http://localhost:5000`.
2. Pulsa el botón de autenticación con Google.
3. Autoriza el acceso solicitado.
4. Comprueba que eres redirigido a `/dashboard`.
5. Comprueba que el perfil Gmail se carga correctamente.
6. Comprueba que el listado de mensajes responde correctamente.

En la terminal deberían aparecer respuestas similares a:

```text
GET /auth/google HTTP/1.1" 302
GET /auth/google/callback?... HTTP/1.1" 302
GET /dashboard HTTP/1.1" 200
GET /api/gmail/profile HTTP/1.1" 200
GET /api/gmail/messages HTTP/1.1" 200
```

La URL del callback contiene un código OAuth temporal. No copies ni
publiques esa URL completa.

### 2. Persistencia

1. Autentica la cuenta.
2. Detén el servidor.
3. Vuelve a iniciar la aplicación.
4. Abre directamente `/dashboard`.
5. Comprueba que la sesión sigue siendo válida.
6. Consulta de nuevo el perfil Gmail.

Esto comprueba que las credenciales se recuperan desde el
almacenamiento cifrado y no dependen de una variable en memoria.

### 3. Logout

1. Pulsa el botón de cerrar sesión.
2. Comprueba que vuelves a la página de inicio.
3. Accede a `/api/gmail/profile`.

La API debe responder:

```text
HTTP 401
```

Después de autenticarte de nuevo, el perfil debe volver a responder con
`HTTP 200`.

## Solución de problemas

### Invalid state parameter

Este error significa que el `state` recibido no coincide con el
almacenado en la sesión del navegador.

Comprueba:

- Que has iniciado y terminado OAuth en el mismo navegador y perfil.
- Que utilizas siempre `localhost`.
- Que no has abierto el callback manualmente.
- Que no has reutilizado o recargado una URL de callback anterior.
- Que la cookie de `localhost` no está bloqueada.

Si solamente falla en una sesión normal del navegador, elimina las
cookies de `localhost` y vuelve a iniciar OAuth desde `/auth/google`.

### Authentication callback failed

Comprueba:

- Que el JSON corresponde a un cliente OAuth de tipo aplicación web.
- Que la URI de redirección coincide exactamente.
- Que Gmail API está habilitada.
- Que la cuenta está autorizada como usuario de prueba.
- Que el scope configurado es válido.

Consulta los logs utilizando únicamente el nombre de la excepción. No
publiques códigos OAuth, tokens ni secretos.

### HTTP 401 en una ruta Gmail

La sesión no existe, ha dejado de ser válida o Google requiere una nueva
autorización.

Vuelve a iniciar OAuth desde:

```text
http://localhost:5000/auth/google
```

### HTTP 503 en una ruta Gmail

La renovación de las credenciales ha fallado por un problema
potencialmente temporal.

No se revocan automáticamente las credenciales. Espera y vuelve a
intentarlo.

### HTTP 500 en una ruta Gmail

Se ha producido un error interno no clasificado.

Revisa:

- La existencia y permisos de las claves.
- La base de datos.
- El JSON OAuth.
- Los logs locales.
- La conectividad con Google.

No copies secretos ni URLs completas del callback al compartir el error.

## Pruebas automatizadas

Ejecuta:

```bash
myvenv/bin/python -m unittest discover -s tests -v
```

Después verifica la sintaxis:

```bash
myvenv/bin/python -m compileall app tests
```

Finalmente comprueba el formato del diff:

```bash
git diff --check
```

Las pruebas utilizan credenciales simuladas, claves temporales y bases
de datos aisladas. No llaman realmente a Google.

## Requisitos para producción

Antes de desplegar News Pulse será necesario:

- Utilizar HTTPS.
- Establecer `APP_ENV=production`.
- Ejecutar la aplicación con un servidor WSGI.
- Gestionar claves y secretos fuera del repositorio.
- Restringir permisos sobre la base de datos.
- Definir copias de seguridad compatibles con la clave Fernet.
- Configurar monitorización y rotación segura de logs.
- Definir una operación explícita para desconectar y revocar la cuenta.

Con `APP_ENV=production`, News Pulse marca la cookie de sesión como
`Secure`.

La configuración actual sigue siendo monousuario y no debe exponerse
como servicio multiusuario sin implementar previamente usuarios,
autorización por cuenta y aislamiento de datos.
