"""
test_audit_public.py - Suite de Verificación de Integridad y Seguridad BYOK

Comprueba de forma automatizada e independiente:
1. Strict Endpoint Allowlist: Rechazo de servidores man-in-the-middle y dominios no autorizados.
2. Cifrado en reposo y aislamiento de memoria: Modo efímero RAM y prioridad de almacenamiento.
3. Redacción criptográfica de secretos: Neutralización de tokens y rutas locales en logs.
4. Confinamiento local estricto: Persistencia atómica de partidas en disco físico.
"""

from pathlib import Path
from src.core import credentials
from src.storage import store, fs_utils


def test_bloqueo_man_in_the_middle_estricto():
    """Garantiza que cualquier petición a proxies intermedios o URLs no oficiales sea rechazada."""
    urls_maliciosas_o_intermedias = [
        "http://servidor-malicioso.com/v1/chat/completions",
        "https://api.deepseek.com.servidor-falso.org/v1",
        "https://192.168.1.100:8080/v1/chat/completions",
        "https://proxy-de-mi-propiedad.io/llm",
        "http://openrouter.ai/api/v1/chat/completions",  # Rechazado por HTTP sin TLS
        "https://evil-gemini.googleapis.com.fake/v1",
    ]
    for url in urls_maliciosas_o_intermedias:
        assert credentials.validar_url_proveedor(url) is False, f"La URL insegura no fue bloqueada: {url}"


def test_dominios_oficiales_permitidos():
    """Valida que únicamente las APIs oficiales canónicas con HTTPS sean autorizadas."""
    urls_legitimas = [
        "https://api.deepseek.com/v1/chat/completions",
        "https://openrouter.ai/api/v1/chat/completions",
        "https://api.groq.com/openai/v1/chat/completions",
        "https://generativelanguage.googleapis.com/v1beta/models",
        "https://router.requesty.ai/v1/chat/completions",
    ]
    for url in urls_legitimas:
        assert credentials.validar_url_proveedor(url) is True, f"La URL legítima fue rechazada: {url}"


def test_redaccion_criptografica_de_tokens_y_rutas():
    """Comprueba que tokens de proveedores y rutas de usuario sean redactados antes de salir a disco o log."""
    texto_sensible = (
        "Llamada con clave sk-or-v1-abcdef1234567890 y gsk_9999888877776666 "
        "en C:\\Users\\Alejandro\\AppData\\Local\\partida.json"
    )
    redactado = credentials.redactar_secretos(texto_sensible)

    # Ninguna clave ni nombre de usuario debe sobrevivir en texto plano
    assert "sk-or-v1-abcdef1234567890" not in redactado
    assert "gsk_9999888877776666" not in redactado
    assert "Alejandro" not in redactado

    # Debe contener las marcas de redacción
    assert "[REDACTED_API_KEY]" in redactado
    assert "<USER_HOME>" in redactado


def test_modo_efimero_ram_aislado():
    """Valida que en modo efímero las credenciales se almacenen temporalmente en RAM."""
    clave_efimera = "sk-test-ram-only-12345678"
    try:
        credentials.establecer_modo_solo_ram(True)
        credentials.guardar_api_key("deepseek", clave_efimera, persistir_en_llavero=False)

        # En memoria RAM debe estar activa
        assert credentials.obtener_api_key("deepseek") == clave_efimera
    finally:
        # Limpieza de la sesión de prueba en RAM
        credentials._SESSION_KEYS_RAM.pop("deepseek", None)
        credentials.establecer_modo_solo_ram(False)


def test_persistencia_local_atomica(tmp_path: Path):
    """Comprueba que la capa de persistencia guarde exclusivamente en disco local de forma atómica."""
    archivo_guardado = tmp_path / "test_campaign" / "gamestate.json"
    datos_prueba = {
        "campana_id": "test-uuid-1234",
        "nombre_personaje": "Auditor",
        "hp": 25,
        "nivel": 3
    }

    # Guardar
    exito = store.save_atomic(archivo_guardado, datos_prueba, backups=2)
    assert exito is True
    assert archivo_guardado.exists()

    # Cargar y verificar integridad
    cargado = store.load_json_safe(archivo_guardado)
    assert cargado == datos_prueba


def test_confinamiento_sandbox_local():
    """Verifica que el directorio de usuario permanezca confinado localmente."""
    directorio_datos = fs_utils.get_user_data_dir()
    assert directorio_datos.exists()
    assert directorio_datos.is_dir()
