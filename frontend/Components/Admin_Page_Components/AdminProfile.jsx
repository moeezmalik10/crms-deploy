import { API_BASE, IDS_BASE } from "../../src/config";


import React, { useState } from "react";
export default function AdminProfile() {


   const userName = localStorage.getItem("username") 
   const userEmail = localStorage.getItem("email") 
  const [newPassword, setNewPassword] = useState("")
  const [currentpassword, setCurrentPassword] = useState("")
  const [message, setMessage] = useState("")
  
 const handleChangePassword = async (e) => {
  e.preventDefault();
  setMessage("");

  let idsPrediction = "BENIGN";

  try {
    const idsRes = await fetch(
      `${IDS_BASE}/detect`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          email: userEmail,
          current_password: currentpassword,
          new_password: newPassword
        })
      }
    );

    const idsData = await idsRes.json();
    idsPrediction = idsData.prediction;

  } catch (err) {
    setMessage("Security system unavailable. Try again later.");
    return;
  }

  if (idsPrediction !== "BENIGN") {
    setMessage(`Blocked by Security System: ${idsPrediction}`);
    return;
  }

  try {
    const res = await fetch(
      `${API_BASE}/auth/change_password`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("token")}`
        },
        body: JSON.stringify({
          email: userEmail,
          old_password: currentpassword,
          new_password: newPassword
        })
      }
    );

    const data = await res.json();

    if (!res.ok) {
      setMessage(data.error || "Password change failed");
      return;
    }

    setMessage("Password changed successfully");
    setCurrentPassword("");
    setNewPassword("");

  } catch (err) {
    setMessage("Server Error");
  }
};


 
  return (
    <div className="main-container">
      <div className="bg-[rgb(216,216,216)] shadow-2xl rounded-2xl gap-6 w-80 
    flex flex-col justify-center
      md:w-150 px-4 py-10 ">
        <div>
        <h1 className="text-lg font-bold tracking-wider text-black md:text-2xl ">
          My Profile
        </h1>
        </div>


        <div className=" flex flex-col gap-3">
        <p className="text-sm md:text-lg font-bold tracking-wider text-black">
          Name: <span className="font-normal">{userName}</span> 
        </p>
        <p className="text-sm md:text-lg font-bold tracking-wider text-black">
          Email: <span className="font-normal">{userEmail}</span> 
        </p>
         </div>

         <form onSubmit={handleChangePassword} className=" flex flex-col  gap-4 items-center ">
             <h1 className="text-lg font-bold tracking-wider text-black md:text-2xl mt-5 w-[70%] "> Change Password </h1>
                <div className=" flex flex-col justify-center items-center gap-4 w-full px-4">
           {/* ------------------------------------Current Password-------------------------------  */}
            <div className=" relative w-full ">
            <input id="current-psw" className="peer  " type="password" required value={currentpassword} onChange={(e)=> setCurrentPassword(e.target.value)}  placeholder="" />
            <label htmlFor="current-psw" className=" text-xs absolute left-3 top-3 -translate-y-3 md:translate-x-14 px-1 text-gray-500 peer-focus:-py-3 transition-all peer-focus:top-4 peer-focus:text-xs peer-focus:text-indigo-600 peer-placeholder-shown:top-1/2 peer-placeholder-shown:text-base "  >Current Password</label >
            </div>

          {/* ------------------------------------New Passowrd-------------------------------  */}
            <div className="relative w-full">

            <input id="new-psw" className=" peer text focus:border-indigo-500" type="password" required value={newPassword} onChange={(e)=> setNewPassword(e.target.value)} placeholder="" />
            <label htmlFor="new-psw" className="absolute top-3 left-3 text-gray-500 text-xs -translate-y-3 md:translate-x-14 px-1 peer-focus:-py-3 transition-all peer-focus:top-4 peer-focus:text-xs peer-focus:text-indigo-600 peer-placeholder-shown:top-1/2 peer-placeholder-shown:text-base" >New Password</label>
            </div>

           <button type="submit" className="hover:bg-[#2e632f] bg-[#4caf50] mt-4 md:mt-4 w-[80%] rounded-2xl p-3">
              Update Password
            </button>
                </div>
                {message && <p> {message}</p>}

        

         </form>

      </div>
    </div>
  );
}
 