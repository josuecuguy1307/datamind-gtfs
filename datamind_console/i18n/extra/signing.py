"""Settings: the autopilot's decision-signing key."""

EN = {
    "settings.signing.title": "Autopilot signing key",
    "settings.signing.development": "No {var} is set: operator decisions are signed with the public development key, so anyone who reads the code could forge those signatures. Set your own key.",
    "settings.signing.configured": "Operator decisions are signed with this installation's own key (from {var}). The value is never shown.",
    "settings.signing.how": "Generate a new key and add it to your .env, then restart the console. Only new decisions use it; recorded ones keep their signature.",
}

ES = {
    "settings.signing.title": "Clave de firma del autopilot",
    "settings.signing.development": "No hay {var} configurada: las decisiones del operador se firman con la clave pública de desarrollo, así que cualquiera que lea el código podría falsificar esas firmas. Configura tu propia clave.",
    "settings.signing.configured": "Las decisiones del operador se firman con la clave propia de esta instalación (de {var}). El valor nunca se muestra.",
    "settings.signing.how": "Genera una clave nueva y agrégala a tu .env; después reinicia la consola. Solo la usan las decisiones nuevas; las registradas conservan su firma.",
}
