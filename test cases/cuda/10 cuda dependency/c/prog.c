#include <cuda_runtime.h>
#include <stdio.h>

int cuda_devices() {
    int result = 0;
    cudaGetDeviceCount(&result);
    return result;
}

int main() {
    int n = cuda_devices();
    if (n == 0) {
        printf("No CUDA hardware found. Exiting.\n");
        return 0;
    }

    printf("Found %i CUDA devices.\n", n);
    return 0;
}
