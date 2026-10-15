# arbitraje

Bot **exclusivamente paper** para estudiar compras de pares Up + Down en mercados Bitcoin de 5 minutos de Polymarket. Usa datos públicos; no contiene firmas, billeteras ni endpoints de envío de órdenes reales.

## Botkeep

1. Crear un servicio Python (3.12 o superior). Importar este repositorio o subir el ZIP **con README.md, requirements.txt y arbitraje/ en la raíz del servicio**.
2. Instalar dependencias: `python -m pip install -r requirements.txt`.
3. Comando de inicio: `python -m arbitraje.app`.
4. En Environment, configurar `DASHBOARD_TOKEN` (mínimo 16 caracteres aleatorios), `DATA_DIR=data` y el `SERVER_PORT` asignado por Botkeep. `.env.example` documenta opciones y la app carga automáticamente el `.env` de Botkeep.
5. Asignar al menos 512 MB RAM. Configurar Domains para acceder al panel por HTTPS. Introducir el token en el panel; no se guarda en el navegador ni se transmite en la URL.
6. Comprobar persistencia de `data/paper.sqlite` tras un reinicio, y exportar backups. No colocar data/ dentro de archivos reemplazados al actualizar código.
7. `/health` responde 200 sólo con libros frescos; 503 significa proceso activo con feed no listo. No configurar reinicios automáticos por 503 sin tolerar fallos transitorios del proveedor.

Botkeep documenta escucha en `0.0.0.0` y `SERVER_PORT`: https://botkeep.cloud/docs/hosting

## Funcionamiento

- Descubrimiento del mercado actual por slug `btc-updown-5m-{timestamp UTC múltiplo de 300}`.
- REST para instantáneas iniciales; WebSocket público con heartbeat, snapshots y deltas de libros, trades y cambios de tick.
- Cotiza una compra post-only virtual en Up o Down sólo con profundidad suficiente y capital para completar el par.
- Modelo maker: volumen delante de la orden al colocarla, sin mejorar prioridad por cancelaciones. Sólo trades SELL en el precio exacto consumen la cola. No se deduce un fill por tocar un precio.
- Fills parciales y fills durante cancelación se agregan a la cobertura. Latencia inicial configurable de 500 ms; es una **hipótesis**, no una medición.
- Cobertura taker con toda la profundidad necesaria y un precio máximo. Puede asumir una pérdida después de fees para reducir exposición.
- Si falla, intenta vender la posición maker tras otra demora. Si no puede salir o cambia el mercado con inventario abierto, conserva la posición pendiente y detiene nuevas entradas. No inventa una valoración de cierre ni asume una resolución.
- Merge virtual libera capital tras 2 segundos (hipótesis), sin rebates.
- Referencia taker+taker: máximo un par de órdenes por mercado, con comprobación retrasada de profundidad. **Es un benchmark idealizado con ejecución simultánea**, no un simulador completo de riesgo de dos piernas taker. Cada estrategia tiene su propio saldo virtual de US$1.000.
- Panel muestra efectivo y PnL realizado, no equity total. Una posición pendiente exige revisar la exposición; PnL realizado positivo no basta para concluir rentabilidad.

## Supuestos y límites de esta primera versión

La comisión usa `C × FEE_RATE × p × (1-p)`, con FEE_RATE=0.07 por defecto conforme a la documentación consultada el 14/10/2026: https://docs.polymarket.com/trading/fees . **No se verifica automáticamente la tasa por mercado**; el panel la identifica como supuesto y hay que confirmar su vigencia antes del estudio.

El paper no modifica el libro real. La prioridad exacta, impacto de nuestras órdenes, fills perdidos durante desconexiones, settlement y costos externos no se conocen. Una reconexión descarta órdenes virtuales pendientes; las posiciones ya registradas sí permanecen. El riesgo de fills durante un corte no se puede reconstruir. Los resultados son estimaciones, requieren validación real pequeña antes de usarse como evidencia económica.

La app conserva estado y los últimos 20.000 eventos en SQLite. **No archiva todos los libros ni ofrece replay completo**. Historial de eventos se recorta; totales persisten. No utiliza precios BTC/Chainlink para decidir la dirección.

Se detienen entradas con libros de más de 3 segundos y durante los últimos 60 segundos. Se respetan mínimo de orden y tick recibidos. Si cambia el esquema de APIs, la app registra el error y no simula operaciones con datos inventados. No soporta automáticamente cualquier futura migración del protocolo.

Para modificar capital o configuración sin mezclar experimentos, usar otro DATA_DIR. Una posición pendiente tras vencimiento permanece detenida y requiere revisión manual de la base/exportación; esta versión no tiene botón para borrarla silenciosamente.

## Ejecutar localmente

```bash
python -m pip install -r requirements.txt
export DASHBOARD_TOKEN='introducir-un-token-aleatorio-largo'
python -m arbitraje.app
```

Abrir http://localhost:8080. En Windows PowerShell usar `$env:DASHBOARD_TOKEN="..."` antes del inicio.

## Validación

```bash
python -m unittest discover -s tests -v
```

Pruebas: comisiones/profundidad, cola, fills parciales, demora de cobertura, conservación de efectivo, cobertura fallida, pérdida de salida, cancelación en tránsito, datos obsoletos, cierre, persistencia y referencia taker+taker.

La conexión pública a Gamma devolvió HTTP 403 en el entorno de preparación; no se certificó una sesión real de feed. La verificación desde Botkeep es el siguiente paso. Este repositorio no está desplegado automáticamente.
