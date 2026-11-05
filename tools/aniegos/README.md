# Reportes recientes de aniegos para Agua Cerca

Recopilación pública con Python 3.9+, sin claves ni dependencias. La app descarga `data/aniegos.json` y vuelve a filtrar los registros vencidos.

```sh
python3 -m unittest discover -s tools/aniegos/tests -v
python3 tools/aniegos/generate.py --output /tmp/aniegos-resultado
python3 tools/aniegos/publish.py --report /tmp/aniegos-resultado/report.json --output data/aniegos.json
```

## Qué se publica

- `incident`: registro revisado en `reviewed.json`, con resumen propio, distrito, lugar publicado, fecha del reporte y fuentes. No se afirma que siga activo. Solo se dibuja si tiene coordenadas referenciales documentadas; nunca se dibuja un polígono de extensión inundada.
- `publication`: artículo descubierto o descargado con fecha reciente, por verificar. La fecha es de publicación, no de ocurrencia. No genera un marcador. La app permite leer el aviso y abrir la fuente.

Los nuevos artículos no se convierten automáticamente en incidentes georreferenciados. La revisión separa los lugares de una misma nota, excluye antecedentes antiguos y documenta la precisión. Las fuentes ya asociadas a un incidente revisado no lo renuevan al vencer. Los estados de atención/resolución requieren una publicación de seguimiento; en esta versión todos los incidentes se muestran como situación actual sin confirmar.

## Fuentes y límites

Se consultan RSS de Defensoría, Caretas, Infobae, La República y El Búho, además del descubrimiento por Google News y URLs conocidas de prueba de RPP y América. Un RSS puede no contener todas las noticias. Los enlaces Google son descubrimiento, no prueba del incidente; no se decodifican usando APIs privadas. Las fechas de los artículos se validan con sus metadatos o el RSS del propio publicador. No se toman coordenadas de la dirección de la redacción del medio.

La ventana máxima es 48 h. Las fechas sin hora se retiran conservadoramente al cumplirse 48 h desde el inicio del día publicado. Se usa America/Lima y se rechazan fechas futuras. Cada ejecución genera un reporte de errores; un fallo total no sobrescribe el archivo público. El archivo anterior puede quedar disponible, pero la app oculta sus registros vencidos y avisa cuando la recopilación no se actualiza durante más de 90 minutos.

Los cuerpos de artículos se procesan en memoria y no se publican. Se conservan enlaces, metadatos, resúmenes propios y títulos de publicaciones pendientes. HTTPS con hosts permitidos también en redirecciones, límite de 3 MB por respuesta, máximo 15 artículos, espera por host y timeout. No se eluden bloqueos ni se reintenta un HTTP 429.

## Operación

El workflow ejecuta pruebas, recopila, genera el JSON y publica solo `data/aniegos.json` en este repositorio. Programado cada 30 minutos (minutos 17 y 47 UTC) y manualmente. GitHub puede retrasar ejecuciones: no es tiempo real. Los artifacts de diagnóstico se conservan siete días. Los cambios del catálogo revisado requieren editar `reviewed.json`; no contienen direcciones privadas de afectados ni cuerpos de noticias.

El GeoJSON del primer piloto sigue siendo un diagnóstico de su antiguo contrato de revisión; la app utiliza exclusivamente `data/aniegos.json`, que admite avisos sin coordenadas. Cobertura parcial: un mapa sin puntos no garantiza ausencia de aniegos.
