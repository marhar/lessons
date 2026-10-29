#include <stdio.h>

void ex1() // some scalar types in memory
{
    int i;
    char c;
    float f;
    long long x;
    char junk;
    printf("i = %d\n", i);
    printf("c = %d\n", c);
    printf("f = %f\n", f);
    printf("x = %lld\n", x);
    printf("sizeof(i) = %ld\n", sizeof(i));
    printf("sizeof(c) = %ld\n", sizeof(c));
    printf("sizeof(f) = %ld\n", sizeof(f));
    printf("sizeof(x) = %ld\n", sizeof(x));
    printf("&i = %p\n", &i);
    printf("&c = %p\n", &c);
    printf("&f = %p\n", &f);
    printf("&x = %p\n", &x);
    printf("&junk = %p\n", &junk);
    i = 42;
    printf("i = %d\n", i);
}

void ex2() // some ints in memory
{
    int i1;
    int i2;
    int i3;
    int i4;
    int i5;
    printf("&i1 = %p\n", &i1);
    printf("&i2 = %p\n", &i2);
    printf("&i3 = %p\n", &i3);
    printf("&i4 = %p\n", &i4);
    printf("&i5 = %p\n", &i5);
}

void ex3() // a struct in memory
{
    struct {
        int i;
        char c;
        float f;
        long long x;
        char junk;
    } ms;
    printf("ms.i = %d\n", ms.i);
    printf("ms.c = %d\n", ms.c);
    printf("ms.f = %f\n", ms.f);
    printf("ms.x = %lld\n", ms.x);
    printf("sizeof(ms.i) = %ld\n", sizeof(ms.i));
    printf("sizeof(ms.c) = %ld\n", sizeof(ms.c));
    printf("sizeof(ms.f) = %ld\n", sizeof(ms.f));
    printf("sizeof(ms.x) = %ld\n", sizeof(ms.x));
    printf("&ms = %p\n", &ms);
    printf("&ms.i = %p\n", &ms.i);
    printf("&ms.c = %p\n", &ms.c);
    printf("&ms.f = %p\n", &ms.f);
    printf("&ms.x = %p\n", &ms.x);
    printf("&ms.junk = %p\n", &ms.junk);
}

void ex4() // pointers
{
    int a = 111;
    int b = 222;

    int * p1;
    int * p2;
    int **p3;

    p1 = &a;
    p2 = &a;

    printf("&a = %p\n", &a);
    printf("p1 = %p\n", p1);
    printf("p2 = %p\n", p2);
    printf("a = %d\n", a);
    printf("*p1 = %d\n", *p1);
    printf("*p2 = %d\n", *p2);

    p3 = &p1;
    printf("p3 = %p\n", p3);
    printf("*p3 = %p\n", *p3);
    printf("a = %d\n", a);
    printf("*p1 = %d\n", *p1);
    printf("**p3 = %d\n", **p3);
}

void ex5() // arrays in memory - two things you can do to an array
{
    // 1. take its address
    // 2. take its size
    // AND NOTHING ELSE... once you have the pointer, all other things are pointer operations
    char a1[10];
    char a2[20];
    int  a3[10];
    printf("&a1 = %p\n", &a1);
    printf("a1 = %p\n", a1);
    printf("a2 = %p\n", a2);
    printf("a3 = %p\n", a3);
    printf("sizeof(a1) = %ld\n", sizeof(a1));
    printf("sizeof(a2) = %ld\n", sizeof(a2));
    printf("sizeof(a3) = %ld\n", sizeof(a3));
}

void ex6() // pointers and arrays
{
    int  a[4];
    a[0] = 999999;
    a[1] = 111;
    a[2] = 122;
    a[3] = 133;
    for (int i = 0; i < 4; ++i)
        printf("a[%d] = %d\n", i, a[i]);

    int *p1;
    char *p2;
    double *p3;

    p1 = a;
    p2 = (char *) a;    // weird, type unsafe
    p3 = (double *) a;

    printf("a     = %p\n", a);  // so many ways to get the same value
    printf("&a    = %p\n", &a);
    printf("&a[0] = %p\n", &a[0]);
    printf("p1    = %p\n", p1);
    printf("p2    = %p\n", p2);
    printf("p3    = %p\n", p3);

    printf("p1+1  = %p\n", p1+1);  // pointers auto-scale
    printf("p2+1  = %p\n", p2+1);
    printf("p3+1  = %p\n", p3+1);

    printf("*p1    = %d\n", *p1);  // crazy data type mangling
    printf("*p2    = %d\n", *p2);
    printf("*p3    = %f\n", *p3);

}

void ex7() // pointer and array manipulation
{
    int a[] = {111, 222, 333};

    int *p;
    p = a;

    printf("sizeof(a) = %ld\n", sizeof(a));
    printf("sizeof(p) = %ld\n", sizeof(p));

    printf("*(a+2) = %d\n", *(a+2));
    printf("*(p+2) = %d\n", *(p+2));
    printf("a[2]   = %d\n", a[2]);
    printf("p[2]   = %d\n", p[2]);

    // a[2] --> *(a + 2) --> *(2 + a) --> 2[a]
    printf("2[a]   = %d\n", 2[a]);
}

void f(int x[])   // equivalent to f(*x)
{
    printf("x[2] = %d\n", x[2]);
    printf("x[10] = %d\n", x[10]);  // are you feeling (un)lucky?
}

void ex8() // pointer and array manipulation
{
    int a[] = {111, 222, 333};
    f(a);  // converts to pointer
}

int main()
{
    printf("\n---- ex1\n"); ex1();
    printf("\n---- ex2\n"); ex2();
    printf("\n---- ex3\n"); ex3();
    printf("\n---- ex4\n"); ex4();
    printf("\n---- ex5\n"); ex5();
    printf("\n---- ex6\n"); ex6();
    printf("\n---- ex7\n"); ex7();
    printf("\n---- ex8\n"); ex8();
}
