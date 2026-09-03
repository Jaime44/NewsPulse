# NewsPulse

## Estado del proyecto

Proyecto en fase P0: estabilización de OAuth y acceso Gmail.

## Requisitos

- Python 3.12
- Proyecto de Google Cloud
- Gmail API habilitada
- Cliente OAuth web

## Instalación

1. Crear `myvenv`.
2. Instalar `requirements.txt`.
3. Copiar `.env.example` como `.env`.
4. Configurar credenciales locales.
5. Ejecutar `python run_web_app.py`.

## Configuración

Tabla con todas las variables de `.env.example`.

## Seguridad

- No subir `.env`, tokens, bases de datos ni `secrets/`.
- Usar permisos `600`.
- No guardar tokens en la sesión del navegador.

## OAuth de Google

- Origen: `http://localhost:5000`
- Callback: `http://localhost:5000/auth/google/callback`
- Scope inicial: `gmail.readonly`

## Limitaciones actuales

- Aplicación monousuario.
- Sin procesamiento de newsletters.
- Sin automatización.
- Sin envío de correo maestro.
