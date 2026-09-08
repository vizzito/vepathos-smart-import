"""El trabajo pesado de Smart Import, sin transporte.

Lo que vive aca no sabe si lo invoco un request HTTP o un mensaje de una cola:
recibe un `WorkerContext` explicito y devuelve o levanta errores de dominio. Esa
es la costura que permite que el mismo codigo corra dentro de la API (un solo
container, el despliegue de hoy) o en un proceso aparte que consume tareas.
"""
