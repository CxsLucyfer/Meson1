#include <stdio.h>
#include "a.h"
#include "b.h"

int main() {
    int life = a_fun() + b_fun();
    printf("%d\n", life);
    return 0;
}
