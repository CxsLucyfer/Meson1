#ifndef MYTHING
#error "Global argument not set"
#endif

#ifdef MYCPPTHING
#error "Wrong global argument set"
#endif

#ifndef MYCANDCPPTHING
#error "Global argument not set"
#endif

int main(int argc, char **argv) {
    return 0;
}
