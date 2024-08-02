---
short-description: Rust language integration module
authors:
    - name: Dylan Baker
      email: dylan@pnwbakers.com
      years: [2020, 2021, 2022]
...

# Rust module

*(new in 0.57.0)*
*(Stable since 1.0.0)*

The rust module provides helper to integrate rust code into Meson. The
goal is to make using rust in Meson more pleasant, while still
remaining mesonic, this means that it attempts to make Rust work more
like Meson, rather than Meson work more like rust.

## Functions

### test(name: string, target: library | executable, dependencies: []Dependency)

This function creates a new rust unittest target from an existing rust
based target, which may be a library or executable. It does this by
copying the sources and arguments passed to the original target and
adding the `--test` argument to the compilation, then creates a new
test target which calls that executable, using the rust test protocol.

This accepts all of the keyword arguments as the
[[test]] function except `protocol`, it will set
that automatically.

Additional, test only dependencies may be passed via the dependencies
argument.

### bindgen(*, input: string | BuildTarget | [](string | BuildTarget), output: string, include_directories: [](include_directories | string), c_args: []string, args: []string, dependencies: []Dependency)

This function wraps bindgen to simplify creating rust bindings around C
libraries. This has two advantages over hand-rolling ones own with a
`generator` or `custom_target`:

- It handles `include_directories`, so one doesn't have to manually convert them to `-I...`
- It automatically sets up a depfile, making the results more reliable


It takes the following keyword arguments

- input — A list of Files, Strings, or CustomTargets. The first element is
  the header bindgen will parse, additional elements are dependencies.
- output — the name of the output rust file
- include_directories — A list of `include_directories` or `string` objects,
  these are passed to clang as `-I` arguments *(string since 1.0.0)*
- c_args — A list of string arguments to pass to clang untouched
- args — A list of string arguments to pass to `bindgen` untouched.
- dependencies — A list of `Dependency` objects to pass to the underlying clang call (*since 1.0.0*)

```meson
rust = import('unstable-rust')

inc = include_directories('..'¸ '../../foo')

generated = rust.bindgen(
    input : 'myheader.h',
    output : 'generated.rs',
    include_directories : [inc, include_directories('foo')],
    args : ['--no-rustfmt-bindings'],
    c_args : ['-DFOO=1'],
)
```

If the header depends on generated headers, those headers must be passed to
`bindgen` as well to ensure proper dependency ordering, static headers do not
need to be passed, as a proper depfile is generated:

```meson
h1 = custom_target(...)
h2 = custom_target(...)

r1 = rust.bindgen(
  input : [h1, h2],  # h1 includes h2,
  output : 'out.rs',
)
```
