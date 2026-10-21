# 🔒 Seguridad y Manejo de Claves API (BYOK - Bring Your Own Key)

Este proyecto funciona bajo el modelo **BYOK (*Bring Your Own Key*)**: tú ingresas tu propia clave de API (DeepSeek, Groq, OpenRouter o Google Gemini) para que el Dungeon Master pueda narrar y evaluar las acciones.

Este repositorio publica, bajo licencia **Source-Available (Solo Lectura y Auditoría)**, únicamente los módulos de **credenciales, red y persistencia local**. El resto del juego (prompts, motor de reglas, interfaz) es privado. Puedes leer ese código para ver cómo se manejan tus claves, pero **el código publicado no prueba por sí solo qué hace el ejecutable que descargaste**: para eso existe la verificación de tráfico de red descrita más abajo, que sí puedes hacer tú mismo.

---

## ⚡ Cómo revisar el código tú mismo (en 30 segundos)

No tienes por qué confiar a ciegas: puedes revisar exactamente qué hace el código en **3 archivos clave**:

### 1. ¿Dónde se guardan mis API Keys? → [`src/core/credentials.py`](src/core/credentials.py)
* **Cero texto plano en el juego:** Cuando guardas tus claves desde la ventana de configuración, se guardan directo en el **Gestor de Credenciales de tu Sistema Operativo** (`Windows Credential Manager` cifrado con DPAPI y atado a tu usuario de Windows, `macOS Keychain` o `SecretService` en Linux) usando la librería estándar `keyring`.
* **Modo Solo RAM (efímero):** Si lo prefieres, puedes activar la casilla *"Modo Efímero (Solo RAM)"*. En este modo, las claves viven solo en la memoria mientras la ventana esté abierta y desaparecen al cerrarla.

* **Botón de Purga Instantánea:** El botón *"🗑️ Borrar todas las claves del equipo"* elimina cualquier secreto tanto de la memoria RAM como del llavero del sistema operativo.

### 2. ¿A qué servidores se conecta el juego? → [`src/llm/gateway.py`](src/llm/gateway.py)
* **Conexión Directa Cliente → Proveedor Oficial (Zero-Backend):** Este juego **no tiene servidores propios, ni proxies, ni telemetría oculta**.
* Antes de enviar cualquier petición HTTP con una cabecera `Authorization` o `x-goog-api-key`, la función `validar_url_proveedor()` en [`src/core/credentials.py`](src/core/credentials.py) verifica que el host destino pertenezca estrictamente a la lista blanca (`ALLOWED_API_HOSTS`):
  * `api.deepseek.com` (DeepSeek Oficial)
  * `openrouter.ai` (OpenRouter Oficial)
  * `router.requesty.ai` (Requesty Router Oficial)
  * `api.groq.com` (Groq Cloud Oficial)
  * `generativelanguage.googleapis.com` (Google Gemini Oficial)
* Cualquier intento de conexión a un dominio no incluido en esa lista o sin cifrado `https://` lanza inmediatamente un ` PermissionError` antes de abrir el socket de red.

### 3. ¿Mis claves pueden filtrarse en los logs o al compartir una partida? → [`src/core/logger.py`](src/core/logger.py) y [`src/storage/feedback_packager.py`](src/storage/feedback_packager.py)
* **Filtro Global de Redacción (`SecretRedactionFilter`):** Todos los logs de consola, archivos rotativos (`logs/dnd_rpg.log`) y trazas de depuración (`saves/{uuid}/debug/`) pasan automáticamente por `redactar_secretos()`.
* **Enmascaramiento Doble:**
  1. Cualquier clave activa configurada en tu sesión es reemplazada literalmente por `[REDACTED_KEY:sk-1***9a]`.
  2. Expresiones regulares detectan patrones de tokens (`sk-...`, `sk-or-v1-...`, `gsk_...`, `AIza...`, `Bearer ...`) y rutas personales del sistema operativo (`C:\Users\TuNombre` → `<USER_HOME>`) y los neutralizan antes de escribir en disco.
* **Si decides compartir una partida (`🧪 ¿Me ayudas a mejorar esto?`):** El `.zip` se arma localmente en tu carpeta `exports/feedback/` pasando todo por `redactar_secretos()`. El juego te abre la carpeta en el explorador de Windows para que **tú mismo puedas abrir el `.zip` y ver qué contiene** antes de mandármelo por correo. El juego nunca sube nada solo a internet.


---

## 🛡️ Verificación del Ejecutable de Itch.io / GitHub Releases

El ejecutable se compila con GitHub Actions desde un repositorio **privado** (necesita el código del juego, que no es público). Por eso **no puedes reconstruir el binario a partir de este repositorio ni verificar su procedencia con attestations**. Lo que sí puedes verificar:

1. **Integridad de la descarga (`SHA256SUMS.txt`):** cada Release publica los hashes SHA-256 de los archivos. Compara el de tu descarga:
   ```powershell
   Get-FileHash .\DnD-Infinite-RPG-win64.zip -Algorithm SHA256
   ```
   Esto confirma que el archivo no se corrompió ni fue alterado en tránsito. **No** prueba qué código contiene el binario.
2. **Comportamiento de red (la verificación que realmente importa):** ejecuta el juego tras un proxy como Mitmproxy, o revisa las conexiones del proceso con `Get-NetTCPConnection`, como se explica en el [README](README.md). Si el ejecutable hiciera algo distinto a lo que muestran `src/llm/gateway.py` y `src/core/credentials.py`, lo verías en el tráfico.
3. **Metadatos (`BUILD_INFO.json`):** incluye versión y fecha de compilación. El commit que menciona pertenece al repositorio privado y no se puede comprobar desde aquí.

---

## 📬 Contacto de Seguridad y Reporte de Vulnerabilidades

Para garantizar la máxima confidencialidad en el reporte de posibles fallos o dudas de seguridad:

1. **Vía Privada Recomendada (GitHub Security Advisories):**
   Utiliza la pestaña privada de reporte de vulnerabilidades en GitHub:  
   👉 [**Reportar Vulnerabilidad en GitHub (Private Advisory)**](https://github.com/alecss75/dnd-infinite-rpg/security/advisories/new)  
   Este canal viaja cifrado de extremo a extremo y no se publica en el historial general.

2. **Contacto Directo:**
   * **Autor:** Alejandro Sansores Arjona
   * **Correo Electrónico:** `alejandrosansoresarjona@gmail.com`
