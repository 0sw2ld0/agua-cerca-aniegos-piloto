# Agua Cerca · reportes recientes de aniegos

Recopilación de publicaciones y reportes revisados, con cobertura parcial. No confirma que un aniego siga activo.

La app consulta [data/aniegos.json](data/aniegos.json). Actualización programada cada 30 minutos mediante Actions, sujeta a retrasos. Los nuevos artículos se muestran por verificar; solo los incidentes revisados con coordenadas documentadas generan puntos referenciales.

Ver [metodología, fuentes y límites](tools/aniegos/README.md).

## Versión web

https://0sw2ld0.github.io/agua-cerca-aniegos-piloto/ (GitHub Pages, carpeta `docs/`). Es estática y de solo lectura, y consulta los datos desde el navegador:

- **Cortes de agua vigentes, horas de agua por sector y puntos de abastecimiento:** GeoSunass. La capa de cortes acepta escrituras públicas; la web solo hace `/query` y muestra el texto con `textContent`.
- **Zonas inundables T100 y puntos críticos ANA:** MINAM.
- **Aniegos recientes, detección en modo sombra y costos de IA:** los JSON de `data/` de este repositorio.
- **Pasos a desnivel:** CENEPRED, con la ubicación referencial del cruce en OpenStreetMap.
- **Búsqueda de direcciones:** Nominatim, solo al pulsar «Buscar» y como máximo una consulta por segundo.

Riesgo por lluvias (SIGRID/CENEPRED) y los cortes de luz no permiten consultas desde el navegador. Quedan para un recolector en el Action.

## Prueba manual con Jev

Workflow **Probar Jev con noticias** (`.github/workflows/jev-prueba.yml`), ejecutable desde Actions → Run workflow. Requiere el secreto de Actions `TYPESAFE_API_KEY`.

- Envía únicamente fragmentos de noticias públicas a TypeSafe (`jev-latest`). Máximo 3 artículos y 2 fragmentos por artículo; límite preventivo US$0.02 por ejecución. El cron consulta Jev únicamente ante evidencia no registrada, como se detalla abajo.
- Clasifica observaciones y selecciona lugares/fechas literales. No usa publicación como fecha del aniego, no crea coordenadas ni publica borradores en `data/aniegos.json`.
- El artefacto `resultado-jev` contiene `report.json` y `RESUMEN.md`, con probabilidades, fechas, lugares, enlaces, consumo y errores. No incluye cuerpos completos de noticias, solicitudes exactas ni secretos. El archivo temporal de auditoría permanece solo en el runner.
- Los fallos de API hacen fallar el job y conservan un informe; una corrida sin novedades termina correctamente y nunca significan «no existen aniegos».
- Los cuerpos de noticias y metadatos son contenido externo no confiable. Las respuestas de Jev se validan; el workflow puede escribir el registro persistente del piloto en el repositorio y los borradores no son publicables.

Esta fase verifica la integración remota. Para publicar automáticamente faltan validación geográfica persistente, evaluación de segmentación/deduplicación y compatibilidad de los estados en iOS.

Contrato de API: https://docs.typesafe.ai/api/system-one . El costo del informe es estimado, no el saldo o factura de TypeSafe.

## Consultar Jev solo cuando cambie la evidencia

El recolector programado cada 30 minutos también consulta el registro persistente `data/jev-state.json`. El registro guarda hashes de las solicitudes y respuestas primitivas validadas, sin cuerpos de noticias, prompts ni secretos. Los workflows manual y programado comparten el grupo de concurrencia `aniegos-feed` y parten del `main` actual para serializar el registro.

- Una solicitud ya resuelta se reutiliza sin llamar a TypeSafe, aunque sus probabilidades sean inciertas o su borrador requiera revisión. Una decisión pendiente no es un motivo para volver a pagar.
- Se normalizan enlaces quitando query y barra final. Cambios de publicidad, HTML externo o fecha de modificación sin cambios en la evidencia seleccionada no crean una consulta nueva. El hash incluye texto seleccionado, título, fecha de publicación, enlace canónico, preguntas y modelo solicitado; una modificación de esos datos se analiza como una versión nueva.
- La selección sigue limitada a dos fragmentos por artículo. Como máximo se procesan tres artículos con consultas nuevas por corrida; los ya conocidos no consumen esos tres cupos. No equivale a detectar todos los cambios de un artículo ni deduplicar semánticamente noticias de distintos medios.
- Un error reserva US$0.004 por prudencia y espera seis horas antes de reintentar esa solicitud. Una respuesta válida libera la reserva y contabiliza el costo estimado por tokens. No hay reintentos inmediatos.
- Límites del piloto: US$0.02 por ejecución, US$0.02 por día de Lima y US$0.10 **acumulados**, incluyendo US$0.00198488 de ensayos previos. No es una cuota diaria ilimitada: al alcanzar el techo, las consultas nuevas quedan diferidas y se sigue reutilizando el registro. Las reservas y estimaciones no sustituyen la factura del proveedor.
- Una corrida sin novedades finaliza correctamente con cero llamadas. La rutina sigue descargando fuentes para descubrir novedades; eso no llama a Jev. Los borradores siguen fuera del feed consumido por iOS.

El registro se escribe antes de cada consulta en el disco del runner y se sube al repo al terminar, también cuando el análisis falla. Una cancelación abrupta o fallo del runner antes de subirlo puede perder la última reserva; el límite es preventivo, no una garantía de facturación transaccional. Los workflows no se cancelan entre sí para reducir ese riesgo. No borrar `jev-state.json`: perderlo permitiría repetir consultas ya pagadas.
