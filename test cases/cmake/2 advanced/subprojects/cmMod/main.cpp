#include <iostream>
#include <zlib.h>
#include "lib/cmMod.hpp"

using namespace std;

int main() {
  cmModClass obj("Hello (LIB TEST)");
  cout << obj.getStr() << " ZLIB: " << zlibVersion() << endl;
  return 0;
}
