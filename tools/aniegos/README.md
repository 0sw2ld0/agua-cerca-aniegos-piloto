# Piloto de reportes recientes de aniegos

Python 3.9+, sin dependencias ni claves. No modifica la app ni publica automáticamente incidentes.

```sh
python3 -m unittest discover -s tools/aniegos/tests -v
python3 tools/aniegos/generate.py --output /tmp/aniegos-resultado
```

Genera `report.json` (descubrimiento, candidatos, rechazos y fallos), `RESULTADO.md` (resumen) y `aniegos.geojson` (solo puntos revisados). Los cuerpos de artículos se procesan en memoria y no se publican. Un hash permite identificar cambios del contenido. `--now` es exclusivamente para pruebas reproducibles, no para renovar eventos.

El RSS oficial de Defensoría descubre artículos nuevos. Google News RSS aporta enlaces para revisión: sus resultados no son eventos ni proporcionan por sí solos una fecha de incidente. Los enlaces Google no se decodifican mediante endpoints no documentados. Las URLs `seed_urls` son una muestra introducida manualmente para probar extractores; no se contabilizan como descubrimiento automático.

La ventana de 48 h de candidatos se aplica a **publicación** para acotar la revisión. No acredita que el evento haya ocurrido en esa ventana. Ningún candidato se convierte automáticamente en aniego activo ni en punto. Una noticia puede describir varios eventos y recordar antecedentes antiguos. Los lugares mencionados son sugerencias sin verificación; no se cuentan como incidentes. El piloto no genera resúmenes con IA ni determina automáticamente fecha de ocurrencia, coordenadas, estado o duplicados semánticos.

Para ensayar exportación revisada, `--reviews archivo.json` admite una lista de objetos con `event_id`, `source_url`, `incident_at` (ISO8601 con zona horaria), `reviewed_at`, `reviewed_by`, `location_label`, `location_precision` (`approximate`/`verified_point`), `coordinates` [longitud, latitud] y `status` (`reported`/`in_attention`/`resolved`/`unconfirmed`). No inventar la hora para completar este contrato experimental: las fechas solo de día necesitan extender el esquema antes de incorporarlas. La exportación rechaza incidentes vencidos aunque la publicación o consulta sea nueva; solo admite fuentes descargadas y validadas en la ejecución actual y deduplica IDs revisados. Los ejemplos de los tests son sintéticos y nunca se exportan al reporte real.

Límites: cobertura parcial, máximo 15 artículos/ejecución, 3 MB/respuesta, HTTPS y hosts permitidos incluso en redirecciones, espera por host, timeout y fallos visibles. No se reintenta un HTTP 429 ni se eluden bloqueos. Un fallo total termina con código 2; una ejecución parcial conserva el detalle de errores. El GeoJSON vacío significa **sin puntos verificados**, no ausencia de aniegos.

## Prueba de Actions

El workflow manual de `workflow.yml` ejecuta las mismas pruebas y el generador, y conserva el resultado como artifact durante siete días. No tiene cron, permisos de escritura, claves ni publicación de un feed para la app. La finalidad es comprobar descarga y generación en GitHub antes de diseñar la extracción de eventos y geocodificación revisada.
