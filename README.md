# Agua Cerca · reportes recientes de aniegos

Recopilación de publicaciones y reportes revisados, con cobertura parcial. No confirma que un aniego siga activo.

La app consulta [data/aniegos.json](data/aniegos.json). Actualización programada cada 30 minutos mediante Actions, sujeta a retrasos. Los nuevos artículos se muestran por verificar; solo los incidentes revisados con coordenadas documentadas generan puntos referenciales.

Ver [metodología, fuentes y límites](tools/aniegos/README.md).

## Prueba manual con Jev

Workflow **Probar Jev con noticias** (`.github/workflows/jev-prueba.yml`), ejecutable desde Actions → Run workflow. Requiere el secreto de Actions `TYPESAFE_API_KEY`.

- Envía únicamente fragmentos de noticias públicas a TypeSafe (`jev-latest`). Máximo 3 artículos y 2 fragmentos por artículo; límite preventivo US$0.02 por ejecución. No hay llamadas a Jev en el cron de publicación.
- Clasifica observaciones y selecciona lugares/fechas literales. No usa publicación como fecha del aniego, no crea coordenadas ni publica borradores en `data/aniegos.json`.
- El artefacto `resultado-jev` contiene `report.json` y `RESUMEN.md`, con probabilidades, fechas, lugares, enlaces, consumo y errores. No incluye cuerpos completos de noticias, solicitudes exactas ni secretos. El archivo temporal de auditoría permanece solo en el runner.
- Los fallos de API o una prueba sin borradores hacen fallar el job y conservan un informe; nunca significan «no existen aniegos».
- Los cuerpos de noticias y metadatos son contenido externo no confiable. Las respuestas de Jev se validan; el workflow solo tiene permiso de lectura del repositorio y los borradores no son publicables.

Esta fase verifica la integración remota. Para publicar automáticamente faltan validación geográfica persistente, evaluación de segmentación/deduplicación y compatibilidad de los estados en iOS.

Contrato de API: https://docs.typesafe.ai/api/system-one . El costo del informe es estimado, no el saldo o factura de TypeSafe.
