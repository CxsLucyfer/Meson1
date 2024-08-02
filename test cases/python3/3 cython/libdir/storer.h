#pragma once

typedef struct _Storer Storer;

Storer* storer_new();
void storer_destroy(Storer *s);
int storer_get_value(Storer *s);
void storer_set_value(Storer *s, int v);
