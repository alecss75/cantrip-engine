# DnD Infinite RPG — Repositorio de Auditoría, Seguridad y BYOK

Hola. Si estás aquí, probablemente viste **DnD Infinite RPG** en [Itch.io](https://alecss75.itch.io/dnd-infinite-rpg) y te dio curiosidad (o desconfianza) ver una aplicación de escritorio que te pide tu propia API Key para jugar rol con IA.

También puedes apoyarme en ko-fi. :D

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/G1A328ZVIH)

Es una desconfianza totalmente válida. Hoy en día abundan las apps que prometen conectar con IA pero en realidad desvían tus peticiones a servidores intermediarios desconocidos, guardan tus contraseñas en texto plano o te meten telemetría hasta en la sopa.

Por eso creé este repositorio. No pretendo regalar meses de trabajo en prompts, balanceo y mecánicas de juego, pero **sí quiero que tengas la certeza matemática de que tu clave está a salvo y de que nadie te está espiando**.

---

## 🎲 ¿Qué es exactamente este juego?

La idea de **DnD Infinite RPG** nació de una frustración personal: cuando intentas jugar rol directamente en ChatGPT o Claude, la IA tiende a inventarse las reglas, alucinar con el daño de las armas y olvidar lo que pasó hace diez minutos.

Para resolverlo, separé el juego en dos cerebros:
1. **La IA es únicamente el narrador (Dungeon Master):** Describe la atmósfera, interpreta a los personajes secundarios y le da vida al mundo.
2. **Python es el árbitro estricto de las reglas:** La matemática de D&D 5e (tiradas de d20 con ventaja/desventaja, iniciativa, cálculo de armadura CA, espacios de conjuro y durabilidad de objetos) la calcula el código de tu computadora de forma determinista. La IA no puede "hacer trampas" ni regalarte una victoria imposible.

Funciona bajo el modelo **BYOK (Bring Your Own Key)**. Puedes conectar tu cuenta gratuita de Google Gemini, Groq (que vuela en velocidad), DeepSeek o enrutadores como OpenRouter. Juegas a precio de coste de proveedor, sin suscripciones mensuales infladas.

---

## 🔒 ¿Qué hay en este repositorio y por qué solo estas partes?

Para que puedas auditar la seguridad sin exponer la lógica propietaria del juego (los orquestadores de prompts y el motor de mecánicas), he liberado aquí el **núcleo de seguridad, red y almacenamiento**:

* [`src/core/credentials.py`](src/core/credentials.py): Aquí puedes ver cómo se guardan tus claves. El juego utiliza el llavero nativo y cifrado de tu sistema operativo (`Windows Credential Manager` vía DPAPI en Windows, `macOS Keychain` o `SecretService` en Linux) a través de la librería estándar `keyring`. Si eres paranoico, incluye una opción de **Modo Solo RAM** que mantiene la clave en memoria volátil y la destruye por completo al cerrar la ventana.
* [`src/llm/gateway.py`](src/llm/gateway.py): El cliente de red. Demuestra que las peticiones HTTP viajan directamente desde tu máquina hacia `api.deepseek.com`, `generativelanguage.googleapis.com`, `openrouter.ai` o `api.groq.com`. Hay una lista blanca estricta (`ALLOWED_API_HOSTS`): si el juego intentara conectar a un dominio no oficial o sin HTTPS, el código aborta la petición de inmediato con un `PermissionError`.
* [`src/storage/store.py`](src/storage/store.py) y [`src/storage/fs_utils.py`](src/storage/fs_utils.py): La persistencia. Verás que las partidas se guardan en formato JSON de forma local y atómica (`os.replace`), dentro de la carpeta del juego o en `%LOCALAPPDATA%`, sin escanear tus documentos personales ni enviar nada a la nube.
* [`tests/test_audit_public.py`](tests/test_audit_public.py): Pruebas automáticas que se ejecutan en los servidores públicos de GitHub Actions cada vez que subo un cambio, comprobando que los filtros contra intermediarios (man-in-the-middle) y la redacción de contraseñas sigan activos.

---

## 🕵️‍♂️ Cómo auditar el tráfico tú mismo (en 2 minutos)

No tienes por qué confiar en lo que dice este texto. Puedes atrapar a la aplicación en tiempo de ejecución:

### 1. Con un proxy de depuración (Mitmproxy, Fiddler, Charles)
El juego respeta las variables de entorno de red del sistema operativo. Si arrancas `mitmproxy` en el puerto 8080:

```powershell
$env:HTTP_PROXY = "http://127.0.0.1:8080"
$env:HTTPS_PROXY = "http://127.0.0.1:8080"
.\DnD-Infinite-RPG.exe
```
Ponte a jugar una partida. Verás en la pantalla del proxy cada petición enviada. Comprobarás con tus propios ojos que **no hay un solo byte saliendo hacia servidores míos ni de analíticas**.

### 2. Revisando conexiones vivas en PowerShell
Si estás jugando y quieres ver con qué servidores está hablando el proceso en tiempo real:
```powershell
Get-NetTCPConnection -OwningProcess (Get-Process DnD-Infinite-RPG).Id | Select-Object RemoteAddress, RemotePort, State
```
Solo verás conexiones HTTPS salientes hacia los rangos de IP oficiales de Google, Cloudflare o DeepSeek.

### 3. Verificar que el ejecutable es auténtico (SHA-256)
Si bajaste el binario desde Itch.io o desde los Releases de GitHub, puedes verificar su integridad calculando el hash:
```powershell
Get-FileHash .\DnD-Infinite-RPG.exe -Algorithm SHA256
```
El resultado debe coincidir con el valor publicado en `SHA256SUMS.txt` del Release. Esto confirma que la descarga no fue alterada, pero **no prueba qué código contiene el binario**: se compila con GitHub Actions desde un repositorio privado. Por eso la verificación que de verdad cuenta es la de los puntos 1 y 2 (tráfico de red). Más detalles en [SECURITY.md](SECURITY.md).

---

## ⚖️ Licencia y Términos

El código en este repositorio está publicado bajo una licencia **Source-Available (Solo Lectura y Auditoría)**:
* Eres libre de descargar, leer, inspeccionar y ejecutar las pruebas de este código en tu máquina para tu uso personal y para evaluar su seguridad. Ten en cuenta que aquí solo está la parte de red, credenciales y persistencia: el juego completo no se puede compilar desde este repositorio.
* Queda estrictamente prohibida la redistribución comercial, reempaquetado, venta o creación de productos derivados a partir de este proyecto. Tienes todos los detalles legales en el archivo [LICENSE](LICENSE).

El contenido de reglas base de Dungeons & Dragons proviene del **System Reference Document 5.1 (SRD 5.1)** de Wizards of the Coast LLC, disponible bajo licencia Creative Commons Attribution 4.0 International (CC-BY-4.0).

---

## 📬 Seguridad y Contacto

Si encuentras cualquier fallo de seguridad o anomalía en el aislamiento de credenciales, puedes reportarlo de forma cifrada y confidencial a través de [GitHub Security Advisories](https://github.com/alecss75/dnd-infinite-rpg/security/advisories/new) o escribirme directamente a `alejandrosansoresarjona@gmail.com`.
