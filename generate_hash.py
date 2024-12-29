from werkzeug.security import generate_password_hash
import sys

def generate_hash(password):
    return generate_password_hash(password)

if __name__ == "__main__":
    # Get password from command line or prompt
    if len(sys.argv) == 2:
        password = sys.argv[1]
    else:
        password = input("Enter password to hash: ").strip()
        
    if not password:
        print("Password cannot be empty")
        sys.exit(1)
    
    hash = generate_hash(password)
    print(f"\nPassword Hash:\n{hash}\n") 