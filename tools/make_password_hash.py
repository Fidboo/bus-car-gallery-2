"""Create the ADMIN_PASSWORD_HASH value for the admin password.

    python tools/make_password_hash.py

Type the password when asked (it is not shown). Copy the long line it prints
into the ADMIN_PASSWORD_HASH setting on the host. The password itself is never
stored anywhere - only this one-way hash.
"""
import getpass
import sys

from werkzeug.security import generate_password_hash


def main():
    pw1 = getpass.getpass("Choose the admin password: ")
    if len(pw1) < 12:
        print("Please use at least 12 characters (a few random words is fine).")
        sys.exit(1)
    if getpass.getpass("Type it again: ") != pw1:
        print("The two passwords did not match.")
        sys.exit(1)
    print("\nADMIN_PASSWORD_HASH value:\n")
    print(generate_password_hash(pw1, method="pbkdf2"))


if __name__ == "__main__":
    main()
